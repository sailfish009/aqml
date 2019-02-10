#!/usr/bin/env python

from itertools import chain, product
import os, sys, re, copy, ase, time
import ase.data as ad
import pickle as pkl
from openeye.oechem import *
import numpy as np
import networkx.algorithms.isomorphism  as iso
import networkx as nx
import cheminfo.oechem.oechem as coo
from cheminfo.molecule.subgraph import *
import cheminfo.rdkit.core as cir
import cheminfo as co
from cheminfo.rw.ctab import write_ctab
from rdkit import Chem
import scipy.spatial.distance as ssd
import cheminfo.openbabel.obabel as cib
import multiprocessing as mt
import cheminfo.core as cic
import cheminfo.math as cim
import deepdish as dd
import itertools as itl
import tempfile as tpf
import cheminfo.graph as cg
from cheminfo.molecule.elements import Elements
import cml.famoneib as fa
import cheminfo.oechem.core as coc

global dsHX
#                 1.10
dsHX_normal = {5:1.20, 6:1.10, \
        7:1.00, 8:0.98, 9:0.92, 14:1.48, 15:1.42, 16:1.34, 17:1.27, 35:1.41, 53:1.61}
dsHX_halved = {}
for key in list(dsHX_normal.keys()): dsHX_halved[key] = dsHX_normal[key]/2.0

# halogen mols
sets_group7 = [ set([1,9]), set([1,17]), set([1,35]), set([1,53]) ]

cnsr = {4:2, 5:3, 6:4, 7:3, 8:2, 9:1, 13:3, 14:4, 15:3, 16:2, 17:1, \
        32:4, 33:3, 34:2, 35:1,  50:4,51:3,52:2,53:1}
tvsr1 = {4:2, 5:3, 6:4, 7:3, 8:2, 9:1, 13:3, 14:4, 15:3, 16:2, 17:1, \
        32:4, 33:3, 34:2, 35:1,  50:4,51:3,52:2,53:1}

rscov0 = Elements().rcs
rsvdw0 = Elements().rvdws

T,F = True,False


class ExtM(object): #coc.newmol):
    """
    extended molecule object
    """
    def __init__(self, zs, coords, bom=None, chgs=None, iasq=[], can0=None):
        na = len(zs)
        nav = (np.array(zs)>1).sum()
        self.nav = nav
        iasvq = []
        for i,iaq in enumerate(iasq):
            zi = zs[i]
            if zi and zi>1:
                iasvq.append(iaq)
        self.iasvq = iasvq
        naq = len(iasq)
        #print('zs=',zs, 'coords=',coords, 'iasq=',iasq)
        if naq>0 and naq<na:
            iasq += [None]*(na-naq)
        self.iasq = iasq
        self.can0 = can0

        self.zs = zs
        self.nheav = nav
        self.coords = coords
        self.bom = bom
        self.chgs = chgs

        #coc.newmol.__init__(self, zs, chgs, bom, coords)

    @property
    def comb(self):
        if not hasattr(self, '_comb'):
            self._comb = [ self.zs, self.coords, self.bom, self.chgs ]
        return self._comb

    @property
    def rdkmol(self):
        """ rdkit mol object """
        ctab = write_ctab(self.zs, self.chgs, self.bom, self.coords)
        return Chem.MolFromMolBlock( ctab, removeHs=F) # plz keep H's

    def generate_coulomb_matrix(self,inorm=False,wz=False,rpower=1.0):
        """ Coulomb matrix
        You may consider using `cml1 instead of `cm """
        na = len(self.zs)
        mat = np.zeros((na,na))
        ds = ssd.squareform( ssd.pdist(self.coords) )
        np.fill_diagonal(ds, 1.0)
        if np.any(ds==0):
            ias1, ias2 = np.where( np.triu(ds==0) )
            print(' ******** found atom pairs with dij=0: ', list(zip(ias1,ias2)), 'zs1=', self.zs[ias1], 'zs2=', self.zs[ias2])
            raise Exception('some atoms are too close!')
        if wz:
            X, Y = np.meshgrid(self.zs, self.zs)
            diag = -1. * np.array(self.zs)**2.4
        else:
            X, Y = [1., 1.]
            diag = np.zeros(na)
        mat = X*Y/ds**rpower
        np.fill_diagonal(mat, diag)
        L1s = np.linalg.norm(mat, ord=1, axis=0)
        ias = np.argsort(L1s)
        self.cm = L1s[ias] if inorm else mat[ias,:][:,ias].ravel()


class molcplx(ExtM):

    """ molecular complex object: a combination of two or more ExtM objects """

    def __init__(self, Ms, keep_atom_order=T):
        self.mols = Ms
        nas = []
        zs = []; coords = []; charges = []; boms = []
        iasv = []; iash = []
        iasq = []; cans = []
        #a2q = []
        for M in Ms:
            zs1, coords1, bom1, charges1 = M.comb
            zs.append( zs1)
            na1 = len(zs1); nas.append(na1)
            coords.append( coords1)
            charges.append( charges1)
            boms.append(bom1)
            iasq += M.iasq
            #iash += M.iash
            #iasi = M.iasq + [None]*(na1-M.naq)
            #a2q += iasi
            cans.append(M.can0)
        zs = np.concatenate( zs )
        coords = np.concatenate(coords, axis=0)
        charges = np.concatenate(charges)
        cans.sort()
        can0 = '.'.join(cans)
        na = sum(nas); nm = len(nas)
        bom = np.zeros((na,na), np.int)
        ias2 = np.cumsum(nas)
        ias1 = np.array([0] + list(ias2[:-1]))
        for i in range(nm):
            ia1 = ias1[i]; ia2 = ias2[i]
            bom[ia1:ia2,ia1:ia2] = boms[i]
        ExtM.__init__(self, zs, coords, bom, charges, iasq=iasq, can0=can0)



class Parameters(object):

    def __init__(self, i3d, fixGeom, k, k2, ivdw, \
                 forcefield, thresh, gopt, M, \
                 reduce_namons, nprocs, igchk):
        self.i3d = i3d
        self.fixGeom = fixGeom
        self.ff = forcefield
        self.k = k
        self.k2 = k2
        self.dsref = None
        self.kmax = max(k,k2)
        self.ivdw = ivdw
        #self.threshDE = threshDE
        self.thresh = thresh
        self.gopt = gopt # geometry optimizer
        #self.iters = iters
        self.reduce_namons = reduce_namons
        self.M = M
        self.nprocs = nprocs
        self.igchk = igchk



class Sets(object):

    def __init__(self, aparam, debug=False):

        self.cans = [] #cans
        self.ms = [] #ms
        self.rmols = [] #rmols
        self.es = [] #es
        self.nsheav = [] #nsheav
        self.ms0 = [] #ms0
        self.maps = [] #maps
        #self.iokgs = []
        self.cms = [] # coulomb matrix
        self.aparam = aparam
        self.debug = debug

    def update(self, ir, mi):
        """
        update `Sets

        var's
        ==============
        mi  -- Molecule info represented as a list
                i.e., [zs, coords, bom, charges]
        """
        zs, coords, bom, charges = mi.comb
        can = mi.can0
        #ds = ssd.pdist(coords)
        #if np.any(ds<=0.5):
        #    print('--zs=',zs)
        #    print('--coords=',coords)
        #    raise Exception('some d_ij very samll!!')
        assert self.aparam.i3d
        ################# for debugging
        #if self.debug:
        write_ctab(zs, charges, bom, coords, sdf='.raw.sdf')
        #################
        iokg = T
        ei = 0.
        ctab = write_ctab(zs, charges, bom, coords)
        m0 = Chem.MolFromMolBlock( ctab, removeHs=False ) # plz keep H's
        rd = cir.RDMol( m0, forcefield=self.aparam.ff )
        m = rd.m
        if not self.aparam.fixGeom:
            iokg, m0, m, ei, coords = self.optg(mi)
            if not iokg:
                print(' ## significant change in torsion detected after optg by rkff for ', can)
                return F
        # torsions didn't change much after ff optg
        rmol = ExtM(zs, coords)
        if self.aparam.M in ['cml1']:
            rmol.generate_coulomb_matrix(inorm=True,wz=False,rpower=1)
        nheav = (zs > 1).sum()
        self.ncan = len(self.cans)
        if can in self.cans:
            ican = self.cans.index( can )
            # for molecule with .LE. 3 heavy atoms, no conformers
            if (not self.aparam.fixGeom) and (not self.aparam.ivdw) and nheav <= 2:
                # but u still need to tell if it belongs to the
                # `ir-th query molecule (so, the amon `m0 might
                # have appeared as an amon of another query molecule
                # considered previously.
                # Note that we use a 3-integer list for labeling the
                # generated amons, i.e., [ir,ican,iconfonmer].
                amon_idx = [ir, ican, 0]
                if amon_idx not in self.maps:
                    self.maps.append( amon_idx )
                    #self.iokgs.append( iokg )
            else:
                ms_i = self.ms[ ican ] # stores the updated geom
                rmols_i = self.rmols[ ican ] # Mols of the same graph (i.e.,conformers)
                                             # with Representation (e.g., cml1) attached
                ms0_i = self.ms0[ ican ] # stores the original geom
                nci = len(ms_i); _ics_i = np.arange(nci)
                es_i = self.es[ ican ]

                inew = True
                if self.aparam.M in ['cml1']: # use difference of energy as citeria
                    xs = np.array([ rmol.cm, ] )
                    ys = np.array([ ma.cm for ma in self.rmols[ican] ])
                    #print(' -- ', xs.shape, ys.shape, can)
                    _drps = ssd.cdist(xs, ys, 'cityblock')
                    #print ' can, _drps = ', can, _drps
                    drps = _drps[0]
                    filt = (drps <= self.aparam.thresh)
                    if np.any(filt):
                        inew = False
                        ics_i = _ics_i[filt]
                elif self.aparam.M in ['e','energy']: # not safe, never use this criteria
                    dEs = np.abs( np.array(es_i) - ei )
                    if np.any( dEs <= self.aparam.thresh ): inew = False
                else:
                    raise Exception('#ERROR: not supported `M')

                if inew:
                    self.ms[ ican ] = ms_i + [m, ]
                    self.rmols[ ican ] = rmols_i + [ rmol, ]
                    self.ms0[ ican ] = ms0_i + [m0, ]
                    self.es[ ican ] = es_i + [ei, ]
                    self.maps.append( [ir, ican, nci] )
                    #self.iokgs.append( iokg )
                else:
                    #icount = 0
                    for ic in ics_i:
                        entry = [ir,ican,ic]
                        if entry not in self.maps:
                            # Note that lines below must exist!!
                            # though it's necessary only for multiple query mols.
                            # I.e., the [ican,ic] may have existed already in `maps,
                            # for the `i-th molecule, but not for the `j-th mol!
                            #icount += 1
                            self.maps.append(entry)
                            #self.iokgs.append( iokg )
                    #if icount > 1: print '#found multiple entries'
        else:
            #m0, m, ei, coords = self.optg(mi)
            self.maps.append( [ir, self.ncan, 0] )
            #self.iokgs.append( iokg )
            self.cans.append( can )
            self.nsheav.append( nheav )
            self.ms.append( [m, ] )
            self.rmols.append( [rmol, ] )
            self.ms0.append( [m0, ] )
            self.es.append( [ei, ] )
            self.ncan += 1
        return T

    def update2(self, ir, can, nheav):
        """
        update mol set if we need SMILES only
        """
        self.ncan = len(self.cans)
        if can not in self.cans:
            #print '++', can #, '\n\n'
            self.maps.append( [ir, self.ncan, 0] )
            #self.iokgs.append( T )
            self.cans.append( can )
            self.nsheav.append( nheav )
            self.ncan += 1
        else:
            ican = self.cans.index( can )
            entry = [ir, ican, 0]
            if entry not in self.maps:
                self.maps.append( entry )
                #self.iokgs.append( T )
        #print(' -- maps = ', self.maps)

    def optg(self, mi):
        """
        post process molecular fragement retrieved
        from parent molecule by RDKit
        """
        #import io2.mopac as im
        import tempfile as tpf
        zs, coords, bom, chgs = mi.comb
        ctab = write_ctab(zs, chgs, bom, coords)
        # get RDKit Mol first
        m0 = Chem.MolFromMolBlock( ctab, removeHs=False ) # plz keep H's
        m1 = Chem.MolFromMolBlock( ctab, removeHs=False ) # plz keep H's
        rd = cir.RDMol( m1, forcefield=self.aparam.ff )

        iokg = T
        if self.aparam.i3d:
          if not self.aparam.fixGeom:
            #if not cg.is_connected_graph(bom):
            #    self.aparam.gopt = 'PM6-D3H4' ##
            #    print(' ** info: aparam.gopt switched to PM6-D3H4')
# the default case, use openbabel to do constrained optimization
            if self.aparam.gopt.lower() in ['obff']:
                ob1 = cib.Mol( ctab, fmt='sdf' )
                ob1.optg_c(iconstraint=3, ff="MMFF94", \
                           optimizer='cg', steps=[30,90], ic=True)
                rd = cir.RDMol( ob1.to_RDKit(), forcefield=self.aparam.ff )
# if u prefer to use rdkit to do FF optimization
# This seems to be a bad choice as it's likely that
# some bugs exist in RDKit code regarding FF opt
# with dihedral constrains in my system. Test it
# yourself for your own system.
            elif self.aparam.gopt.lower() in ['rkff']:
                if self.aparam.reduce_namons:
                    #print('now do constrained optg')
                    rd.optg_c(2.0,300) #300) #dev=2.0,maxIters=self.aparam.iters[0]) #200) #20)
                    rd.optg(maxIters=900) #900)
                    if self.aparam.igchk:
                        iokg = rd.iokg
                    #print('now do a further optg wo constraint')
                else:
                    rd.optg_c(2.0,60) #1200)
            elif self.aparam.gopt.lower() in ['xtb']:
                rd.optg_c(2.0,60)
                rd.optg_xtb(acc='normal', nproc=self.aparam.nprocs)
                if self.aparam.igchk:
                    iokg = rd.iokg
# if u prefer to do a partial optimization using PM7 in MOPAC
# for those H atoms and their neighboring heavy atoms
            elif self.aparam.gopt.lower() in ['pm7','pm6','pm6-d3h4']: #do_pm6_disp:
                # in case it's a molecular complex
                rd.optg2(meth=self.aparam.gopt, iffopt=T)
                if self.aparam.igchk:
                    iokg = rd.iokg
            else:
                raise Exception('#error: unknow geometry optimizer')
        if hasattr(rd, 'energy'):
            e = rd.energy
        else:
            e = rd.get_energy()
        m = rd.m

        newm = coc.newmol(rd.zs, rd.chgs, rd.bom, rd.coords, \
                          dsref=self.aparam.dsref, \
                          scale_vdw=self.aparam.scale_vdw)
        if newm.icrowd_cov:
            fdt = './crowded/ffopt' # Temporary folder
            if not os.path.exists(fdt): os.mkdir(fdt)
            tsdf = tpf.NamedTemporaryFile(dir=fdt).name + '.sdf'
            print(' -- ', tsdf)
            rd.write_sdf(tsdf)
            iokg = F
            #raise Exception('#ERROR: too crowded!!')
        return iokg, m0, m, e, rd.coords

    def _sort(self):
        """ sort lms (the list of mols) """
        maps = np.array(self.maps)
        ncan = len(self.cans)
        seqs = np.arange(ncan)
        nsheav = np.array(self.nsheav)
        ncs = [ len(ms_i) for ms_i in self.ms ]
        cans = np.array(self.cans)
        nsheav_u = []
        ncs_u = []
        seqs_u = []
        cans_u = []
        ms_u = []; ms0_u = []

        # now sort the amons by the number of heavy atoms
        for i in range(1, self.aparam.kmax+1):
            seqs_i = seqs[ i == nsheav ]
            cans_i = cans[ seqs_i ]
            seqs_j = seqs_i[ np.argsort(cans_i) ]
            seqs_u += list( seqs_j )
            for j in seqs_j:
                cans_u.append( cans[j] )
                ms_j = self.ms[j]; ms0_j = self.ms0[j]
                ncj = len(ms_j)
                ncs_u.append( ncj )
                nsheav_u.append( nsheav[j] )
                ms_u.append( ms_j ); ms0_u.append( ms0_j )

        seqs_u = np.array(seqs_u)

        # now get the starting idxs of conformers for each amon
        ias2 = np.cumsum(ncs_u)
        ias1 = np.concatenate( ([0,],ias2[:-1]) )

        # now get the maximal num of amons one molecule can possess
        print(' size of maps: ', maps.shape)
        irs = np.unique( maps[:,0] ) # sorted now
        nt = len(irs) # 1+maps[-1,0];
        namons = []
        for i in irs: # range(nt):
            namon = (maps[:,0] == i).sum()
            namons.append(namon)
        namon_max = max(namons)

        # `maps2 stores the amon idx for each target molecule
        # (Note: any conformer is an amon)
        maps2 = -1 * np.ones((nt, namon_max),dtype=int)
        for i,ir in enumerate(irs): #range(nt):
            filt_i = (maps[:,0] == ir)
            maps_i = maps[filt_i, 1:]
            jcnt = 0
            for j in range(namons[i]):
                jcan, jc = maps_i[j,:] # `jcan: the old idx of can
                jcan_u = seqs[ seqs_u == jcan ] # new idx of can
                maps2[i, jcnt] = ias1[jcan_u] + jc
                jcnt += 1
        self.ms = ms_u
        self.ms0 = ms0_u
        self.cans = cans_u
        self.nsheav = nsheav_u
        self.ncs = ncs_u
        self.maps2 = maps2

    def _sort2(self):
        """ sort lms (list of mols) for i3d = False"""
        maps = np.array(self.maps)
        ncan = len(self.cans)
        seqs = np.arange(ncan)
        nsheav = np.array(self.nsheav)
        cans = np.array(self.cans)

        nsheav_u = []
        seqs_u = []
        cans_u = []
        # now sort the amons by the number of heavy atoms
        for i in range(1, self.aparam.k2+1):
            seqs_i = seqs[ i == nsheav ]
            cans_i = cans[ seqs_i ]
            seqs_j = seqs_i[ np.argsort(cans_i) ]
            seqs_u += list( seqs_j )
            for j in seqs_j:
                cans_u.append( cans[j] )
                nsheav_u.append( nsheav[j] )

        seqs_u = np.array(seqs_u)

        # now get the maximal num of amons one molecule can possess
        irs = np.unique( maps[:,0] ) # sorted now
        nt = len(irs) # 1+maps[-1,0];
        namons = []
        for i in irs: #range(nt):
            namon = (maps[:,0] == i).sum()
            namons.append(namon)
        namon_max = max(namons)

        # `maps2 stores the amon idx for each target molecule
        # (Note: any conformer is an amon)
        maps2 = -np.ones((nt, namon_max)).astype(np.int)
        for i,ir in enumerate(irs): #range(nt):
            filt_i = (maps[:,0] == ir)
            maps_i = maps[filt_i, 1:]
            jcnt = 0
            for j in range(namons[i]):
                jcan, jc = maps_i[j,:] # `jcan: the old idx of can
                jcan_u = seqs[ seqs_u == jcan ] # new idx of can
                maps2[i, jcnt] = jcan_u
                jcnt += 1
        self.cans = cans_u
        self.nsheav = nsheav_u
        self.maps2 = maps2
        self.ncs = np.ones(ncan).astype(np.int)


def is_subset(a, b):
    """
    a = [1,2], b = [[2,4], [2,1], [3,9,10], ]
    is `a a subset of `b? Yes
    Order of elements in a list DOES NOT matter
    """
    iok = False
    for si in b:
        if set(si) == set(a):
            iok = True
            break
    return iok


class atom_db(object):
    def __init__(self, symbol):
        wd = 'data/atoms/'
        symbs = ['B','C','N','O','Si','P','S', 'F','Cl','Br']
        assert symb in symbs, '#ERROR: no such atomic data?'
        self.mol = coo.StringM( 'data/%s.sdf'%symb ).mol


def cmp(a,b):
    if a < b:
        return -1
    elif a > b:
        return 1
    else:
        return 0

class ParentMol(coo.StringM):

    def __init__(self, string, stereo=F, isotope=F, iat=None, i3d=T,\
                 iasp2arout=T, icc4Rsp3out=T, nogc=T, ichg=F, irad=F,  \
                 k=7, k2=7, opr='.le.', nocrowd=T, scale_vdw=1.0, \
                 ioc=T, iocn=F, icrl2o=T, irddtout=T, ixtb=F, \
                 forcefield='mmff94', fixGeom=F, keepHalogen=F, \
                 iextl=F, ivdw=F, inmr=F, debug=F, iwarn=T, \
                 nprocs=1, warning_shown=F):
        self.warning_shown=warning_shown
        self.iextl = iextl
        self.scale_vdw = scale_vdw
        self.ixtb = ixtb
        self.ioc = ioc
        self.nprocs = nprocs
        self.iocn = iocn
        self.icrl2o = icrl2o #
        self.irddtout = irddtout

        ds = None; pls = None
        coo.StringM.__init__(self, string, stereo=stereo, isotope=isotope, \
                             ds=ds, pls=pls, scale_vdw=scale_vdw, nprocs=nprocs)
        if os.path.exists(string) and self.na > 100: # save `ds and `pls
            ftmp = string[:-4]+'.npz'
            if os.path.exists(ftmp):
                print(' ++ read `ds and `pls from file: ', ftmp)
                dt = np.load(ftmp)
                self._ds = dt['ds']
                self._pls = dt['pls']
            else:
                ds = self.ds #ssd.squareform( ssd.pdist(self.coords) )
                pls = self.pls #cg.Graph(self.g, nprocs=self.nproc
                print(' ++ save `ds and `pls to file ', ftmp)
                np.savez(ftmp, ds=ds, pls=pls)
        self.iwarn = iwarn
        self.k = k
        self.k2 = k2
        self.forcefield = forcefield
        self.fixGeom = fixGeom
        self.nogc = nogc
        self.iasp2arout = iasp2arout
        self.icc4Rsp3out= icc4Rsp3out
        self.nocrowd = nocrowd
        self.iat = iat
        self.keepHalogen = keepHalogen
        self.debug = debug
        self.vsa = {'.le.': [-1,0], '.eq.': [0, ]}[opr] # values accepted
        self.i3d = i3d
        self.irad = irad
        self.ivdw = ivdw
        #if not inmr:
        #    #ncbs = []
        #    #if self.i3d and self.ivdw:
        #    #    ncbs = self.ncbs #perceive_non_covalent_bonds()

    def i_ct0ex2vb_persist(self, iasq, newm):
        """
        Is the vdw bond (present in query) retained in `subm?
        """
        iok = F
        if self.ats_ict0ex2vb:
            #print('    Found in query conj ats w dang=0 & 2 end atoms rij < sum of rvdw')
            _atsq = []
            for _atsi in newm.ats_ict0ex2vb:
                _atsq.append( iasq[_atsi] )
            if np.any([ set(_atsj) in self.ats_ict0ex2vb for _atsj in _atsq ]):
                #print('    And this amon contains such local env')
                PL0 = np.max(newm.pls[newm.iasv][:,newm.iasv])
                if PL0 <= 3:
                    iok = T
                else:
                    if np.all(newm.iconjs==1) and PL0<=5:
                        iok = T
                #if iok:
                #    print('    found strained & conj env like \\\\__// in query, ')
                #    print('    keep it as an amon!')
                #else:
                #    print('    could not find strained & conj env like \\\\__// ')
                #    print('    in query for this amon. Now skip!')
            else:
                pass
                #print('    found local structure \\\\__// or alike, where')
                #print('    two end H atoms are too close and which do not')
                #print('    exist in the query molecule. Drop it!')
        return iok


    def generate_amons(self,submol=None):
        """
        generate all canonial SMARTS of the fragments (up to size `k)
        of any given molecule
        """
        #if self.irad:
        #    raise Exception(' Radical encountered! So far it is not supported!')

        debug = self.debug
        cans_u = []
        nrads = 0

        for seed in generate_subgraphs(self.b2a, self.a2b, k=self.k, submol=submol):
            lasi_rel = list(seed.atoms)
            _lasi = self.iasv[lasi_rel] # convert to absolute atomic idx
            lbsi = list(seed.bonds)
            lasi = list(_lasi)
            bs = []
            for ib in seed.bonds: # relative atomic idx
                #print('ib=', ib, self.b2a[ib])
                b = list(self.b2a[ib])
                bs.append( list(self.iasv[b]) ) # convert to absolute atomic idx
            nheav = len(lasi)
            iaq2iaa = dict(list(zip(lasi,list(range(nheav)))))

            # constraint on number of heavy atoms
            if cmp(nheav, self.k) not in self.vsa:
                continue

            zs = self.zs[lasi]
            assert np.all(zs>1), '#ERROR: H showed up?'

            if self.iat != None: # assume input idx starts from 1
                _ja = self.iat - 1
                _zj = self.zs[_ja]
                if _zj > 1:
                    jok = ( _ja in lasi ) # self.iat is an idx starting from 1
                # otherwise, check if this H atom is connected to any heavy atom in `lasi
                else:
                    jok = False
                    for _ia in lasi:
                        if self.bom[_ia, _ja] > 0:
                            jok = True
                            break
                if not jok:
                    continue

            ats = set(lasi)
            chgs = self.chgs[lasi] # to be retained throughout
            zs = self.zs[lasi]
            cns = self.cns[lasi]
            cnshv = self.cnsv[lasi]
            tvs = self.tvs[lasi]

            # get the coords_q and cns before matching SMARTS to target
            coords = self.coords[lasi]

            iconjs = self.iconjs[lasi]

            #print('##1 nheav=',nheav)

            ifd_extl= F

            # maximal number of ring atoms to be kept
            ndic_conj = {3:1, 4:1, 5:4, 6:4, 7:4}
            ndic_nonconj = {3:1, 4:2, 5:3, 6:3, 7:3}

            if sum(chgs) != 0:
                continue

            lasi2 = [] # idx of H's (in query) bonded to heavy atoms
            if nheav == 1:
                # keep HX, X=F,Cl,Br,I??
                zi = zs[0]
                symb1 = cic.chemical_symbols[zi]
                if symb1 in ['F','Cl','Br','I',]:
                    if not self.keepHalogen:
                        continue
                    #else:
                    #    zsi = np.array([zi, 1], dtype=int)
                    #    chgs = np.array([0, 0], dtype=int)
                    #    bom = np.zeros((2,2))
                    #    bom[0,1] = bom[1,0] = 1
                    #    coords = np.array([[0.,0.,0.], [dsHX_normal[zi], 0., 0.]])
                    #    can = symb1 #'[%s][H]'%symb1
                    #    mi = ExtM(zsi, coords, bom=bom, chgs=chgs, iasv=lasi, iash=lasi2, can=can)
                    #    yield mi
                    #    continue
                cni = cns[0]
                chgi = chgs[0]
                tvi = tvs[0]
                #if (tvsr1[zi] != tvi) or (cnsr[zi] != cni) # PH5 is forbidden
                if tvi!=cni or (chgi!=0): # radical. Note: now PH5 is allowed!
                    if not self.irad: # irad: accept radical or not
                        #print ' ia, zi, cni, chgi = ', lasi[0],zi,cni,chgi
                        continue
                boms = [ np.zeros((1,1),dtype=int) ]
            else:
                sg = np.zeros((nheav,nheav), np.int)
                for bij in bs:
                    ia,ja = list(bij)
                    i,j = lasi.index(ia),lasi.index(ja)
                    sg[i,j] = sg[j,i] = 1
                ## check isomorphism
                _sg = self.g[lasi,:][:,lasi]
                if not np.all(_sg==sg):
                    #print '##iso not satisfied'
                    continue

                # initialize `_bom to `sg
                _bom = sg.copy()
                cnsi = sg.sum(axis=0)
                nsh = cns - sg.sum(axis=0) # numbers (of) hydrogens (in the fragment)
                vsi = _bom.sum(axis=0)
                dvsi = tvs - (vsi+nsh)

                # get aromatic status of atoms in query
                iars = self.aromatic[lasi]
                # get mesomeric status of atoms in query
                imeso = self.gmeso.meso[lasi_rel]

                # radical check
                #irad = F
                if (sum(zs)+sum(nsh))%2 != 0:
                    #irad = T # n_elec is odd -> a radical
                    nrads += 1 #print(' #Found 1 radical!' )
                    continue

# now filter out amons as described in the description section within class ParentMols()
                i_further_assessment = F
                if (self.iextl) and (nheav > 7):
                    gt = cg.Graph(_bom)
                    ## rectify (PO4)n, it turns out that a dual unit is not necessary, i.e.,
                    ## we need subm of HO-P(=O)(O)-O-P(=O)(O)-OH, consisting of 8 heavy atoms
                    ## at most. The 9-atom entity can be totally described by its consistitugin
                    ## smaller unit.
                    for i in range(nheav):
                        if zs[i] in [8]: # [7,8]
                            # 1 being added below corresp. to atom `i
                            nai = 1 + (np.logical_and(gt.pls[i]>0, gt.pls[i]<=2)).sum()
                            if nai<=8 and nai==nheav:
                                # the latter criteria is crutial to remove redundant
                                # amons. E.g., when (CH3)3C-O-C(CH3)3 is selected,
                                # the redundant (CH3)3C-O-C(C)(C)CC may also be selected.
                                ifd_extl = T
                                break

                    if not ifd_extl:
                        continue

# neglect any fragment containing multivalent atom whose degree of heavy atom differs
# from that in query by `nhdiff and more.
# E.g., when nhdiff=2, given a query O=P(O)(O)O, only O=[PH](O)O is
# to be kept, while O=[PH3], O=[PH2]O will be skipped
                ifdmulval = F
                nhdiff = 3
                dcnshv = cnshv - cnsi
                for j in range(nheav):
                    if (zs[j] in [15,16]) and (dcnshv[j] >= nhdiff):
                        ifdmulval = T
                        break
                if ifdmulval:
                    continue

# keep >C=O
                iok = T
                if self.ioc:
                    for oc in self.ocs:
                        _oc = set(oc)
                        atsc = _oc.intersection(ats)
                        if atsc:
                            if len(atsc) != 2:
                                iok = F
                                break
                if not iok:
                    #print('ias=',lasi)
                    continue

# even more agressive, keep >C(=O)N< or >C(=O)N=
                iok = T
                if self.iocn: # default: False
                    for ocn in self.ocns:
                        _ocn = set(ocn)
                        if _ocn.intersection(ats):
                            if not (_ocn.issubset(ats)):
                                iok = F
                                break
                if not iok:
                    continue

# never break a conjugated N-membered ring by removing 1 or 2 atom? T/F
                #rNs_exclude = [3,4,5] # it turns out that 5-membered ring has to survive!!
                iok = T
# for conjugated ring, e.g., a benzene ring, part of which
# may be "cccc". Geometry opt will end up with
# two H's very close to each other, not representative env,
# thus for 6-ring, a fragment consisting of at most 3 atoms
# is allowed

# for non-conjugated ring, use criteria above
                if self.icrl2o: #6a4:
                    for rN in self.rings:
                        #print('rN=', rN)
                        nar = len(rN)
                        atsi = list(rN)
                        share = rN.intersection(ats) # rN is already a python set
                        nshr = len(share)
                        ndff = nar-nshr
                        if np.all(self.iconjs[atsi]==1):
                            if nshr > ndic_conj[nar] and nshr < nar:
                                iok = F
                                break
                            #else:
                            # for non-conj ring, we leave the decision to be made
                            # at a later time when `bom is complete
                if not iok:
                    continue


# first retain the BO's for bonds involving any multi-valent atom, i.e.,
# atom with dvi>1. Here are a few examples that are frequently encountered:
# 1) C2 in "C1=C2=C3" & "C1=C2=N3";
# 2) N2 and N3 in "C1=N2#N3" ( -C=[N+]=[N-], -N=[N+]=[N-] )
# 3) '-S(=O)(=O)-', -Cl(=O)(=O)=O,
# By doing This, we can save a lot of work for BO perception later!
                #print 'tvs = ', tvs, ', vsi=',vsi+nsh, ', dvsi=', dvsi
                iasr_multi = []
                for _i, _ia in enumerate(lasi):
                    #if np.any([ iia in list(tsi) for tsi in self.envsC ]):
                    #    dvsi[ii] = 0
                    if dvsi[_i] > 1:
                        iasr_multi.append(_i)
                        for _ja in self.ias[self.bom[_ia]>1]:
                            if np.any(_ja==_lasi):
                                _j = iaq2iaa[_ja]
                                _bom[_i,_j] = _bom[_j,_i] = self.bom[_ia,_ja]

                # update dvsi for the 1st time
                vsi = _bom.sum(axis=0)
                dvsi = tvs - (vsi+nsh)
                #print 'dvsi = ', dvsi
                #print 'tvs = ', tvs, ', vsi=',vsi, ', nsh=',nsh
                #print 'bom = ', _bom

                # check if valence of multi-valent atoms are alright!
                # e.g., for O=C=C=C=O
                # when `lasi =  [1, 2], tvs, tvsi, dvsi =  [4 4] [3 3] [1 1]
                # is an invalid amon
                #print ' iasr_multi = ', iasr_multi
                if len(iasr_multi) > 0:
                    if np.any(dvsi[iasr_multi]!=0):
                        #print ' ** multi'
                        continue

                if np.any(dvsi>1):
                    #print 'dvi>1, implying say, [Cl][O] in query: -ClO3'
                    continue
                #print('\n')

                # now perceive double bonds
                #print('#########  zsi = ', zs)
                ge = coc.EGraph(_bom, tvs, nsh)
                #iok, boms = update_bom(_bom, tvs, nsh)
                if not ge.istat:
                    continue
                boms = ge.boms

                # get mesomeric status of this amons
                imeso_a = np.array([F]*nheav, dtype=bool)
                if ge.ats_meso:
                    for ia0 in ge.ats_meso:
                        imeso_a[ia0] = T

            # get coords of H's
            coords2 = []
            nh = 0
            icnt = nheav
            bsxh = [] # X-H bonds
            bsxh_visited = []

            for _i in range(nheav):
                ia = lasi[_i]
                _nbrs = np.setdiff1d(self.ias[self.bom[ia]>0], lasi)
                for ja in _nbrs:
                    bond = set([ia,ja])
                    if (bond not in bsxh_visited) and (self.zs[ja] == 1):
                        #print('nh=',nh, 'bond=',bond)
                        bsxh_visited.append(bond)
                        bxh = [_i,icnt]
                        bsxh.append(bxh)
                        lasi2 += [ja]
                        coords2.append( self.coords[ja] )
                        icnt += 1
                        nh += 1

            for _i in range(nheav):
                ia = lasi[_i]
                _nbrs = np.setdiff1d(self.ias[self.bom[ia]>0], lasi)
                for ja in _nbrs:
                    bond = set([ia,ja])
                    if (bond not in bsxh_visited) and (self.zs[ja]>1):
                        #print('nh=',nh, 'bond=',bond)
                        bsxh_visited.append(bond)
                        bxh = set([_i,icnt])
                        bsxh.append(bxh)
                        dsHX = dsHX_normal #if self.fixGeom else dsHX_halved
                        if self.i3d:
                            coords_i = self.coords[ia]
                            v1 = self.coords[ja] - coords_i
                            #print(' ** ia, ja, v1 = ', ia, ja, v1)
                            dHX = dsHX[self.zs[ia]]
                            coords_j = coords_i + dHX*v1/np.linalg.norm(v1)
                        else:
                            coords_j = np.array([0., 0., 0.])
                        coords2.append(coords_j)
                        icnt += 1
                        nh += 1
            #print('bsxh=', bsxh)
            if nh > 0:
                coords = np.concatenate((coords,coords2))
            chgs = np.concatenate((chgs,[0]*nh))
            zsa = np.concatenate((zs,[1]*nh))

            nat = nheav + nh
            #mols = []
            ishown = T
            for _bom in boms:
                bom = np.zeros((nat,nat),dtype=int)
                bom[:nheav,:nheav] = _bom
                # append H's
                for _bxh in bsxh:
                    _ia,_ja = _bxh
                    bom[_ia,_ja] = bom[_ja,_ia] = 1
                # final check
                tvs_heav = bom.sum(axis=0)[:nheav]
                #print ' tvs_heav = ', tvs_heav
                #print ' tvs = ', tvs
                if not np.all(tvs_heav-tvs==0):
                  if self.iwarn and (not self.warning_shown):
                    self.warning_shown = T
                    #print(' ** [warning] ** ')
                    #print('      Not all dvs==0 for the subg found, check!')
                    #print('      This may happen when input is rad, but from which')
                    #print('      it is still possible to extract non-rad amons ')
                    #print('      Example: c1(ccc(cc1)[C](C#N)C#N)[C](C#N)C#N')
                    #print('             |   ')
                  continue

                #print(' +++ dsref=', self.rsmax)
                newm = coc.newmol(zsa, chgs, bom, coords, \
                                  dsref=self.rsmax, \
                                  scale_vdw=self.scale_vdw)
                try:
                    can = newm.can #OECreateSmiString(_newm, OESMILESFlag_Canonical)
                except:
                    # write xyz block to dispaly
                    si = ''
                    for i,zi in enumerate(zsa):
                        x,y,z = coords[i]
                        si += '%2s %12.6f %12.6f %12.6f\n'%(co.chemical_symbols[zi], x,y,z)
                    #print(si)
                    raise Exception('#ERROR: mol to can failed')

                if '.' in can:
                    continue

                #print(' ## can = ', can)

                if can in cans_u:
                    if (nheav <= 2) and (not self.fixGeom) and (not self.ivdw):
                        continue

                ## Now we have SMILES, we can resume from where we
                ## left last time
                if ifd_extl:
                    #print('can=', can)
                    newm = coo.StringM(can) #_newm)
                    if newm.is_conj_amon:
                        ifd_extl = T
                    else:
                        continue
                if ifd_extl: # and (can not in cans_u):
                    pass #print(' ##### found larger essential amons with N_I=%d: %s'%(nheav, can))
                    #ishown = F


                # check iconjs
                ats_cr5_a = newm.ats_cr5
                if nheav>7:
                    if np.all( np.logical_and(newm.iconjsv>0, newm.iconjsv<=2) ):
                        if len(ats_cr5_a) > 0:
                            ats_nr_a = np.setdiff1d(newm.iasv, ats_cr5_a) # non-ring atoms
                            if newm.gv[ats_cr5_a][:,ats_nr_a].sum()==1: #(newm.iconjs[ats_nr_a]!=1).sum() >= 1:
                                #print('    # N_I>7 & 5-ring, found only one -R group, skip!')
                                continue

                        if (newm.iconjsv==2).sum()>2:
                            #print('   # N_I>7, found >2 atoms with iconj==2, skip!')
                            continue

                        #if len(newm.ats_ict0ex2vb)>=3:
                        if len(newm.ats_ic4ex2c)>=3:
                            #print('   # N_I>7, found tors that are `ic4ex2c >=3 times, skip!')
                            continue
                    else:
                        #print('   # N_I>7, but some atom has iconj=0 or >2, skip!')
                        continue

                # check if atoms in 5-membered conjugated ring is connected to atoms
                # that are mesomeric in Q
                ats_meso_q = self.gmeso.ats_meso
                if len(ats_cr5_a)>0:
                    #print('    # found 5-membered aromatic ring')
                    istop = F
                    for ia5 in ats_cr5_a:
                        nbrs_i = np.setdiff1d(newm.iasv[ newm.gv[ia5]>0 ], ats_cr5_a)
                        if len( np.intersect1d(_lasi[nbrs_i], ats_meso_q) ) >0: # ar: aromatic ring
                            #print('    found >=1 atoms (in 6-ar of Q) attached to 5-ar, skip!')
                            istop = T
                            break
                    if istop:
                        continue

                # check mesomeric status
                # This will remove amons like C=c1ccccc1=C
                if nheav>7 and newm.iconjr:
                    iasr_a = list(newm.iasr56)
                    iasr_l = list(set(newm.ias).difference(set(iasr_a)))
                    iarsq = iars[iasr_a]
                    imesoq = imeso[iasr_a]
                    if newm.aromatic.sum() != iarsq.sum():
                        #print(' #### %s aromatic states of some atoms have changed, skip!'%can)
                        continue
                    na_meso = imeso_a.sum()

                    if np.all(imesoq) and (na_meso==0):
                        if can not in ['C=c1ccccc1=C']: ###### need for a final call to determine if this is necessary
                            #print('    # found C=c1ccccc1=C or structure alike, skip!' )
                            continue
                        #print('    imeso_a = ', imeso_a, 'imesoq=', imesoq)

                    if na_meso > 0:
                        if na_meso != imesoq.sum():
                            #print(' #### %s : mesomeric state of some atoms changed, skip!'%can)
                            continue
                        else:
                            # now we deal with Ph-CH=CH2 and structures alike
                            if self.i3d and na_meso==6:
                                _patt = '[a]~[a]~[^2;A]=[^2;A]'
                                ifd, idxs, _ = coc.is_subg(newm.mol, _patt, iop=1)
                                if ifd:
                                    #if newm.cnsv[idxs[0][-1]] == 1:
                                    #    print('    found Ph[^2]=[O;S], skip!')
                                    #    continue
                                    sets = [ set(i4s) for i4s in newm.ats_ic4ex2c ]
                                    #print('    found PhCH=CH2 or alike')
                                    if np.all(imeso[idxs[0][-2:]]):
                                        # the two last atoms are in mesomeric env in the query mol, keep this
                                        # amon so as to account for conjugation effects!
                                        pass #print('    where a) all atoms were mesomeric in q, keep it!')
                                    elif np.any([ 90>newm.get_dihedral_angle(i4s)>30 for i4s in idxs ]):
                                        if set(newm.zs[newm.gmeso.ats_meso]) != set([6]):
                                            #print('    found hetero-nuc meso ring (e.g., c1cccnc1), geom turns flat after optg, skip!')
                                            continue
                                        #print('    where b) some tor has an angle > 30 degree, keep it!')
                                    elif np.all([ set(i4s) not in sets for i4s in idxs ]):
                                        # E.g., Ph-CH=NH, where H points outwards of the plane formed by ats_v
                                        pass #print('    where no `ats_ic4ex2c exists, keep it!')
                                    else:
                                        #print('    neither a) or b) is satisfied, skip!')
                                        continue


                # now tell if `istrain has changed for atoms in the non-conj rings
                iok = T
                if self.icrl2o: #6a4:
                    for rN in self.atsr_strain: #r6 in self.atsr6:
                        nar = len(rN)
                        atsi = list(rN)
                        _share = rN.intersection(ats) # rN is already a python set
                        share = list(_share)
                        nshr = len(share)
                        ndff = nar-nshr
                        if not np.all(self.iconjs[atsi]):
                            if nshr > ndic_nonconj[nar] and nshr < nar:
                                iaas = [ iaq2iaa[iia] for iia in share ]
                                #if np.sum(self.istrains[share] != newm.istrains[iaas]) > 1:
                                if np.any(self.istrains[share] != newm.istrains[iaas]):
                                    iok = F
                                    break
                if not iok:
                    #print('## strain status changed: ', can)
                    continue

                # check if it's C=CC=C-R (R=N,O,..., but C)
                if self.icc4Rsp3out:
                    #_patt = '[^2]~[^2]~[^2]~[^2]-[^3]'
                    #ifd, idxs, _ = coc.is_subg(newm.mol, _patt, iop=1)
                    #if ifd and len(newm.zsv)==5 and set(newm.zsv)!=set([6]):
                    #    print('   found [^2]~[^2]~[^2]~[^2]-[^3], but it is not C=CC=CC, skip!')
                    #    continue

                    # check \\__// and its derivatives
                    if newm.ats_cc4bent and newm.i_ats3_conn_cc4bent:
                        #print('   found \\__// and its derivatives, skip!')
                        continue

                # check if atoms are too crowded in 3d geom
                if self.i3d and self.nocrowd:
                    if newm.icrowd_cov:
                        newm.write_ctab(None, './crowded/')
                        fot = os.path.basename(newm.opf)
                        #print('    Crowded due to too close cov bond, geom to ./crowded/%s'%fot)
                        continue

                    if self.nogc: # no geometry clash due to H's attached to ats_ict0ex2
                        ats_q = [ set(sai) for sai in self.ats_ict0ex2gc ]
                        ats_a = [ set(_lasi[sai]) for sai in newm.ats_ict0ex2gc ]
                        iclash = F
                        if ats_a:
                            if ats_q:
                                istats = []
                                for idx in ats_a:
                                    istat = (idx not in ats_q)
                                    istats.append(istat)
                                # report the state iclash as T if none of the sets of
                                # atoms in subm is found to be clashed in query (q).
                                # As for other cases (e.g., one clashed torsion found in
                                # both subm and q, another not found in q but in subm,
                                # we leave it to be dealt with later when calling function
                                # `self.i_ct0ex2vb_persist()
                                if np.all(istats):
                                    iclash = T
                            else:
                                iclash = T
                        if iclash:
                            #print('   geometry clash found in amons, but not in query. Skip!')
                            continue

                # now kick out amons that are redundant (i.e., flexible), for which
                # it's easy to achieve high accuracy once trained by even smaller amons
                # if newm.irddt and newm.icrowd:
                if self.irddtout:
                    if newm.irddt:
                        #print('    mol is too rddtible')
                        if not self.i3d:
                            continue
                        else:
                            if newm.irigid:
                                continue
                            #if newm.nheav > 7:
                            #    continue
                            ivb = ( len(newm.ncbs)>0 and newm.nheav<=7 \
                                    and newm.plvmax>=4 )
                            if newm.ats_ncopl:
                                if newm.i_ncopl_rddt:
                                    #if not ivb:
                                    continue
                                #else:
                                #    print('    but contains non-coplanar conj bond, keep!')
                            else:
                                #continue
############################################################
                                if not ivb:
                                    continue
############################################################
                    #else:
                    #    if self.i3d:
                    #        #if len(newm.ncbs)>0:
                    #        #    print('    mol is rigid with vdw bond, gone after optg; skip!')
                    #        #    continue
                    #        #else:
                    #        print('    mol is rigid and contains no vdw bond. keep!')

                cans_u.append( can )

                #print('    \To be accepted/')

                mi = ExtM(zsa, coords, bom=bom, chgs=chgs, iasq=lasi+lasi2, can0=can)
                if submol is None:
                    yield mi
                else:
                    yield [zsa,chgs,bom,coords]



class Logger(object):

    def __init__(self, obj=None):
        if obj in ['stdout', None]:
            fid = None
            isnull = T
        else:
            assert isinstance(obj,str)
            fid = open(obj,'w')
            isnull = F
        self.isnull = isnull
        self.fid = fid

    def write(self, string):
        if self.isnull:
            print(string)
        else:
            self.fid.write(string+'\n')

    def close(self):
        if not self.isnull:
            self.fid.close()


class params(object):

    def __init__(self, strings, reduce_namons=F, fixGeom=F, iat=None, wg=T, i3d=T, \
                 iwa=T, k=7, iprt=T, submol=None, label=None, stereo=F, isotope=F, \
                 iextl=F, substring=None, rcut=6.0, imap=T, k2=7, \
                 opr='.le.', M='cml1', iclean=T, \
                 scale_vdw=1.0, thresh=0.01, wsmi=T, keepHalogen=F, nprocs=1, \
                 forcefield='mmff94', gopt='rkff', ixtb=F, nocrowd=T, \
                 icc4Rsp3out=F, nogc=F, iasp2arout=T, \
                 ioc=T, iocn=F, icrl2o=T, igchk=T, irddtout=F, \
                 ivdw=F, ivao=F, nmaxcomb=3, reuseg=F, saveg=F, \
                 irad=T, ichg=T, prefix='', iwarn=T, debug=F, log=T):
        self.strings = strings
        self.reduce_namons = reduce_namons
        self.fixGeom = fixGeom
        self.iat = iat
        self.wg = wg
        self.i3d = i3d
        self.iwa = iwa
        self.k = k
        self.iprt = iprt
        self.submol = submol
        self.label = label
        self.stereo = stereo
        self.isotope = isotope
        self.iextl = iextl
        self.substring = substring
        self.rcut = rcut
        self.imap = imap
        self.k2 = k2
        self.opr = opr
        self.M = M
        self.iclean = iclean
        self.scale_vdw = scale_vdw
        self.thresh = thresh
        self.wsmi = wsmi
        self.keepHalogen = keepHalogen
        self.nprocs = nprocs
        self.forcefield = forcefield
        self.gopt = gopt
        self.ixtb = ixtb
        self.nocrowd = nocrowd
        self.icc4Rsp3out = icc4Rsp3out
        self.nogc = nogc
        self.iasp2arout = iasp2arout
        self.ioc = ioc
        self.iocn = iocn
        self.icrl2o = icrl2o
        self.igchk = igchk
        self.irddtout = irddtout
        self.ivdw = ivdw
        self.ivao = ivao
        self.nmaxcomb = nmaxcomb
        self.reuseg = reuseg
        self.saveg = saveg
        self.irad = irad
        self.ichg = ichg
        self.prefix = prefix
        self.iwarn = iwarn
        self.debug = debug
        self.log = log


def genamons(p):
    """
      for multiprocessing
    """
    obj = ParentMol(p.string, iat=p.iat, i3d=p.i3d, k=p.k, k2=p.k2, stereo=p.stereo, \
                    isotope=p.isotope, scale_vdw=p.scale_vdw, opr=p.opr, fixGeom=p.fixGeom, \
                    nocrowd=p.nocrowd, icc4Rsp3out=p.icc4Rsp3out, ioc=p.ioc, iocn=p.iocn, ixtb=p.ixtb, \
                    nogc=p.nogc, iasp2arout=p.iasp2arout, iextl=p.iextl, \
                    icrl2o=p.icrl2o, irddtout=p.irddtout, irad=p.irad, ichg=p.ichg, ivdw=p.ivdw, \
                    keepHalogen=p.keepHalogen, debug=p.debug, iwarn=p.iwarn, \
                    warning_shown=p.warning_shown, forcefield=p.forcefield, nprocs=p.nprocs)
    lms = [] # list of mols
    if not obj.istat:
      print(' [failure to parse SMILES/kekulization]: %s'%obj.can)
    else:
      if obj.is_radical():
        if iwarn:
          print(' ** warning: input mol is a radical')
        if not p.irad:
          return lms #raise Exception(' Consider setting irad=T... [Todo: full support of radical]')
      if obj.has_standalone_charge():
        if p.iwarn:
          self.io.write(' ** warning: input mol is charged species')
        if not p.ichg:
          return lms #raise Exception(' Consider setting ichg=T... [Todo: full support of charged species]')
      try:
          for mi in obj.generate_amons(submol=submol):
              #if i3d:
              lms.append(mi)
      except:
          return lms #raise Exception('#ERROR: `generate_amons() failed!!')
    return lms


class ParentMols(object):

    def __init__(self, strings, reduce_namons=F, fixGeom=F, iat=None, wg=T, i3d=T, \
                 iwa=T, k=7, iprt=T, submol=None, label=None, stereo=F, isotope=F, \
                 iextl=F, substring=None, rcut=6.0, imap=T, k2=7, \
                 opr='.le.', M='cml1', iclean=T, \
                 scale_vdw=1.0, thresh=0.01, wsmi=T, keepHalogen=F, nprocs=1, \
                 forcefield='mmff94', gopt='rkff', ixtb=F, nocrowd=T, \
                 icc4Rsp3out=F, nogc=F, iasp2arout=T, \
                 ioc=T, iocn=F, icrl2o=T, igchk=T, irddtout=F, \
                 ivdw=F, ivao=F, nmaxcomb=3, reuseg=F, saveg=F, \
                 irad=T, ichg=T, prefix='', iwarn=T, debug=F, log=T):
        """
        prefix -- a string added to the beginning of the name of a
                  folder, where all sdf files will be written to.
                  It should be ended with '_' if it's not empty
        iclean -- remove redundency in retrieved submols, defaulted to T
                  For debugging purpose, e.g., to visualize the retrieved
                  local submol, set it to T
        log    -- write log file

        ixtb   -- use xtb geom later? T/F

        nocrowd-- avoid sub-structure with too close atoms (i.e., r_ij < 1.25*(r_vdw^i +r_vwd^j)

        imap   -- write maps? T/F
        iwa    -- write graph amons? T/F

        iextl  -- include larger amons with N_I > 7
                  This will reset k from 7 (the default) to 9 and then
                  filter out those amons that meet the following two conditions:
                  i) at least one O atom is involved
                  ii) There are no more than 7 heavy atoms in the local
                      fragment enclosing the O atom and all its neighboring
                      atoms within a radius of PathLength=2

        ivao   -- return vdw amons only??

        submol -- specify a substructure specifically, should be a
                  list of format [atoms,bonds]. Useful for diagnose
                  purposes.

        substring -- SMILES of a ligand.
                  Typically in a protein-ligand complex, we need
                  to identify the ligand first and then retrieve
                  all the local atoms that bind to the ligand via
                  vdW interaction as amons for training in ML. The
                  thus obtained fragment is dubbed `centre.

                  If `substring is assigned a string,
                  we will generated only amons that are
                  a) molecular complex; b) any atom in the centre
                  must be involved.
        rcut   -- cutoff radius centered on each atom of the central
                  component. It's used for determination of valid vdw amon.

        iat    -- idx of atom. If specified, any fragment centered on this
                  atom, covering all atoms within a radius of `rcut will be
                  selected as the AMONs
        """

        k0 = k
        if iextl and k0 < 9:
            iextl = T
            k = 9
        else:
            iextl = F
        self.iextl = iextl

        self.debug = debug

        # label name
        if label is None:
            s1 = 'EQ' if opr == '.eq.' else ''
            svdw = ''
            if ivdw:
                svdw += '_vdw%d'%k2
            scomb = '-comb2' if nmaxcomb == 2 else ''
            if ivdw: svdw += scomb
            sthresh = '-dM%.2f'%thresh if thresh > 0 else ''
            if ivdw: svdw += sthresh
            if prefix == '':
                fdn = 'g%s%d%s'%(s1,k,svdw)
            else:
                fdn = prefix
            if self.iextl:
                fdn += '-extl'
            self.fd = fdn
            if iat is not None:
                fdn += '-iat%d'%iat # absolute idx
            fcan = fdn + '/' + fdn + '.can'
            h5f = '%s/map.h5'%fdn
        else:
            if label in ['auto']:
                label = 'g%d'%k0
                if ivdw: label += 'v'
                if ivao: label += 'o'
                if ivdw:
                    label += '-nc%d'%nmaxcomb
                xtra = '-rat' if reduce_namons else ''
                label += xtra
                if self.iextl:
                    label += '-extl'
            fcan = label + '.can'
            h5f = label + '/map.h5'
            fdn = label
            for fd in [fdn, fdn+'/i-raw/']:
                if not os.path.exists(fd):
                    os.mkdir(fd)

        fin = None
        if log and (label is not None):
            fin = label + '.out'
        self.io = Logger(fin)


        # use mmff94 only, as there are some unresolved issues with uff,
        # one typical example is that for some structure, uff in rdkit
        # tend to favor a structure with two H's overlapped!!
        # You may manually verify this by calling optg_c1() and optg() for
        # a substructure C=CC(C=C)=C retrieved from Oc1ccc(C=O)cc1
        assert forcefield == 'mmff94', '#ERROR: DO NOT USE UFF'

        self.k = k
        self.k2 = k2

        # igchk : check geometry after optg by ff
        aparam = Parameters(i3d, fixGeom, k, k2, ivdw, \
                           forcefield, thresh, \
                           gopt, M, reduce_namons, nprocs, \
                           igchk)

        ncpu = mt.cpu_count()
        if nprocs > ncpu:
            nprocs = ncpu

        # temparary folder
        tdirs = ['/scratch', '/tmp']
        for tdir in tdirs:
            if os.path.exists(tdir):
                break

        # num_molecule_total
        assert type(strings) is list, '#ERROR: `strings must be a list'
        nmt = len(strings)
        if iat != None:
            assert nmt == 1, '#ERROR: if u wanna specify the atomic idx, 1 input molecule at most is allowed'

        cans = []; nsheav = []; es = []; maps = []
        ms = []; ms0 = []

        # initialize `Sets
        ids = []
        seta = Sets(aparam)
        warning_shown = F

        #### aaaa
        ps = []
        for ir in range(nmt):
            string = strings[ir]
            psi = params(string, iat=iat, i3d=i3d, k=k, k2=k2, stereo=stereo, \
                         isotope=isotope, scale_vdw=scale_vdw, opr=opr, fixGeom=fixGeom, \
                         nocrowd=nocrowd, icc4Rsp3out=icc4Rsp3out, ioc=ioc, iocn=iocn, ixtb=ixtb, \
                         nogc=nogc, iasp2arout=iasp2arout, iextl=iextl, \
                         icrl2o=icrl2o, irddtout=irddtout, irad=irad, ichg=ichg, ivdw=ivdw, \
                         keepHalogen=keepHalogen, debug=debug, iwarn=iwarn, \
                         forcefield=forcefield, nprocs=nprocs)
            ps.append(psi)
            #aparam.dsref = obj.rsmax
            #aparam.scale_vdw = scale_vdw
            ids.append(ir)

        pl = mt.Pool(processes=nprocs)
        objs = pl.Pool(genamons, psi)
        for ir in range(nmt):
            lms = objs[ir]
            if len(lms) == 0:
                continue

            #lms = [] # list of mols
            cansi = []
            nasv = []

            # now amons generation
            if i3d:
                for mi in lms:
                    cansi.append(mi.can0)
                    nasv.append(mi.nav)
            else:
                for mi in lms:
                    if mi.can0 not in cansi:
                        cansi.append(mi.can0)
                        nasv.append(mi.nav)

            ngi = len(set(cansi)) # number of graphs (i.e., unique smiles)
            nmi = len(lms) # len(cansi)
            #print('  ++ found %d cov amons'%nmi)
            if debug: print('ngi,nmi=',ngi,nmi, ' unique cans=', set(cansi))

            nasv = np.array(nasv, np.int)

            ncan = len(cansi)
            # now remove redundancy
            if iclean:
                if i3d:
                    # some mol may need to be removed, because they underwent significant change in geom after rkff optg
                    cansi2 = []
                    for i in range(ncan):
                        tfi = seta.update(ir, lms[i])
                        if tfi:
                            cansi2.append( cansi[i] )
                else:
                    cansi2 = cansi
                    if imap:
                        for i in range(ncan):
                            seta.update2(ir, cansi[i], nasv[i])
            # now update cans
            for ci in cansi2:
                if ci not in cans:
                    cans.append(ci)
        #### aaaa


        ncan = len(cans)
        #print('cans=',cans)

        # sort all amons
        if not i3d:
            #assert label is not None
            if imap:
                print(' -- now sort amon SMILES by N_I')
                seta._sort2()
                cans = seta.cans
                self.cans = cans
                if label is not None:
                    h5f = label + '.h5'
                    dd.io.save(h5f, {'ids': np.array(ids,dtype=int), 'maps': seta.maps2} )

            self.cans = cans
            if iwa:
              assert label is not None, '#ERROR: please specify `label'
              fcan = label+'.can'
              with open(fcan, 'w') as fid:
                fid.write('\n'.join( [ '%s'%(cans[i]) for i in range(ncan) ] ) )
        else:
            if not iclean: # debug
                self.ms = []
                self.iass = []
                for im,mi in enumerate(lms):
                    self.ms.append(mi) #m)
                    _ias = mi.iasvq
                    _ias.sort()
                    self.iass.append(tuple(_ias))
            else:
                seta._sort()
                cans = seta.cans; ncs = seta.ncs; nsheav = seta.nsheav
                self.cans = cans
                nd = len(str(ncan))

                if wg:
                    dd.io.save(h5f, {'ids': np.array(ids,dtype=int), 'maps': seta.maps2} )

                _ms = seta.ms; _ms0 = seta.ms0
                self.maps = seta.maps2
                self.nm = sum(ncs)
                if wg:
                    print(' amons are to be written to %s'%fdn)
                    self.io.write(' %9s   %6s %9s %9s %60s'%('#NI', '#im', '#nc', '#ic', '#SMILES'))
                    for i in range(ncan):
                        ms_i = _ms[i]; ms0_i = _ms0[i]
                        nci = ncs[i]
                        labi = '0'*(nd - len(str(i+1))) + str(i+1)
                        self.io.write('%9d   %06d %9d %9d %60s'%(nsheav[i], i+1, nci, sum(ncs[:i+1]), cans[i]))
                        if ivao: print('%9d   %06d %9d %9d %60s'%(nsheav[i], i+1, nci, sum(ncs[:i+1]), cans[i]))
                        for j in range(nci):
                            f_j = fdn + '/frag_%s_c%05d'%(labi, j+1) + '.sdf'
                            f0_j = fdn + '/i-raw/frag_%s_c%05d_raw'%(labi, j+1) + '.sdf'
                            m_j = ms_i[j]; m0_j = ms0_i[j]
                            Chem.MolToMolFile(m_j, f_j)
                            Chem.MolToMolFile(m0_j, f0_j)
                else:
                    ms = []; ms0 = []
                    for i in range(ncan):
                        ms += _ms[i]
                        ms0 += _ms0[i]
                self.ms = ms
                self.ncan = ncan
                self.ms0 = ms0
                print(' ## summary: found %d molecular graphs, %d configurations'%(ncan, self.nm) )




if __name__ == "__main__":

    import sys, time

    args = sys.argv[1:]
    idx = 0
    ob = F
    if '-ob' in args: ob = T; idx+=1
    rk = F
    if '-rk' in args: rk = T; idx+=1
    ivdw = F
    if '-ivdw' in args: ivdw = T; idx += 1
    wg = F
    if '-wg' in args: wg = T; idx += 1
    reduce_namons = F
    if '-reduce_namons' in args: reduce_namons = T; idx += 1

    t0 = time.time()
    #objs = args[idx:]
    _args = args[idx:]
    n = len(_args)
    if n == 0:
        objs = ["C=C=S(=C=C)=[N+]=[N-]", \
                "S1(=C)(C)=CC=CC=C1", \
                "[N+]1([O-])=CC=C[NH]1", \
                "C[N+](=O)[O-]", \
                "C=[NH+][O-]", \
                "C[N+]#[C-]", \
                "C[NH2+][C-](C)C", \
                "[CH-]=[O+]C", \
                "N=[NH+][NH-]", \
                "[NH-][NH+]=C1C=C(C=C)C=C1", \
                "OP(=S)=P(=[PH2]C)C", \
                "O[N+]([O-])=[N+]([N-]C)O", \
                "OC12C3C4C1N4C32"] # the last one is highly strained, may have problem in acquring g0
                #"[NH3+]CC(=O)[O-]", \
                #"C[O-]",\
                #"C[NH3+]",\
    elif n == 1:
        f = _args[0]
        if f[-3:] in ['smi','can']:
            objs = [ si.strip() for si in file(f).readlines() ]
        else:  # either an xyz file or a SMILES string
            objs = _args
    else:
        objs = _args

    css = []
    for obj in objs:
        a = ParentMols([obj], reduce_namons, fixGeom=F, iat=None, wg=wg, i3d=i3d,\
                    k=7, label='temp', k2=7, opr='.le.', wsmi=T, irc=T, nocrowd=T, \
                   M='cml1', thresh=0.1, \
                   keepHalogen=F, forcefield='mmff94', gopt=gopt, \
                   ivdw=ivdw)
        css.append(a.cans)
    for i,cs in enumerate(css):
        print('## ', objs[i])
        print(css[i])
    print(' -- time elaped: ', time.time()-t0, ' seconds')

