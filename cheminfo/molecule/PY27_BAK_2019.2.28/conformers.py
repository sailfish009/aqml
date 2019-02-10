#!/usr/bin/env python

import io2, os, sys
from rdkit import Chem
from rdkit.Chem import AllChem, TorsionFingerprints
from rdkit.Geometry.rdGeometry import Point3D
from rdkit.ML.Cluster import Butina
import cheminfo.openbabel.obabel as cob
import scipy.spatial.distance as ssd
import numpy as np
from io2.gaussian_reader import GaussianReader as GR0
#import io2.xyz as ix
from cheminfo.molecule.molecule import *
from cheminfo.molecule.nbody import NBody
from cheminfo.rw.xyz import write_xyz
from cheminfo.base import *
import cheminfo.molecule.amon_f as cma
import tempfile as tpf
import cheminfo.rdkit._rdkit as crk
try:
    import representation.x as sl
except:
    pass

h2kc = io2.Units().h2kc
T, F = True, False
np.set_printoptions(formatter={'float': '{: 0.4f}'.format})

_hyb = { Chem.rdchem.HybridizationType.SP3: 3, \
            Chem.rdchem.HybridizationType.SP2: 2, \
            Chem.rdchem.HybridizationType.SP: 1, \
            Chem.rdchem.HybridizationType.UNSPECIFIED: 0}

bt2bo = { Chem.BondType.SINGLE:1.0,
          Chem.BondType.DOUBLE:2.0,
          Chem.BondType.TRIPLE:3.0,
          Chem.BondType.AROMATIC:1.5,
          Chem.BondType.UNSPECIFIED:0.0}

bo2bt = { '1.0': Chem.BondType.SINGLE,
          '2.0': Chem.BondType.DOUBLE,
          '3.0': Chem.BondType.TRIPLE,
          '1.5': Chem.BondType.AROMATIC,
          '0.0': Chem.BondType.UNSPECIFIED}

class _atoms(object):
    """ `atoms object from file formats other than xyz"""
    def __init__(self, f):
        import ase.io as aio
        m = aio.read(f)
        self.zs = m.numbers
        self.coords = m.positions
        self.na = len(self.zs)


def get_torsions(m):
    """ get torsion types & dihedral angles
    returns a dictionary {'1-6-6-8':[100.], } """
    mr = RawMol(m.zs, m.coords)
    mr.connect()
    g = mr.g
    obj = NBody(m.zs, m.coords, g, unit='degree', iheav=T, icn=T)
    obj.get_dihedral_angles()
    dic = obj.mbs4
    return dic


class CM(object):
    """
    coulomb matrix object
    """
    def __init__(self, atoms, param={'M':'cml1','rp':1.,'wz':T,'sort':T}):
        self.param = param
        self.atoms = atoms
        self.cml1 = T if param['M'] in ['cml1'] else F

    def generate_coulomb_matrix(self):
        """ Coulomb matrix

        sorted CM has serious limitation when used to identify unique conformers.
        E.g., for CH3-CH3 molecule, as the L1 norm of all H-containing columns are
        the same, so shuffling such columns leads to different CM, though the molecule
        remains unchanged

        The limitation can be alleviated through the use of L1 norm of each column!!
        """
        atoms = self.atoms
        na = atoms.na
        mat = np.zeros((na,na))
        _ds = ssd.squareform( ssd.pdist(atoms.coords) )
        dsp = _ds**self.param['rp']
        np.fill_diagonal(dsp, 1.0)
        zs = atoms.zs
        _X, _Y = np.meshgrid(zs,zs)
        if self.param['wz']:
            mat = _X*_Y/dsp
            diag = -np.array(zs)**2.4
        else:
            mat = 1/dsp
            diag = np.zeros(na)
        np.fill_diagonal(mat,  diag)
        if self.param['sort']:
            L1s = np.array([ np.sum(np.abs(mat[i])) for i in range(na) ])
            ias = np.argsort(L1s)
            if self.cml1:
                x = L1s[ias]
            else:
                x = np.ravel(mat[ias,:][:,ias])
        else:
            x = np.ravel(mat)
        #print 'x = ', x
        return x


def cdist(objs, param={}):
    _param = {'M':'cml1','rp':1.0,'sort':T,'wz':F}
    for key in param.keys():
        if key in _param.keys():
            if param[key] != _param[key]:
                _param[key] = param[key]
    _xs = []
    nc = len(objs)
    for obj in objs:
        if _param['M'] in ['cm','cml1']:
            _xobj = CM(obj,_param)
            xi = _xobj.generate_coulomb_matrix()#; print '              xi = ', xi
            _xs.append( xi )
        else:
            raise '#ERROR: unknown `M'
    xs = np.array(_xs)
    return xs, ssd.squareform( ssd.pdist(xs,'cityblock') )


class AmonDict(object):
    """
    stores all amons (a dictionary) for a dataset
    and look up the dictionary for relevant AMONs
    when a query molecule is specified

    Note that we assume AMONs are associated with
    xyz files of the same format as in class `OptedMols.
    In particular, the second line has to read like:
        'E=%.5fHa,rawfile=%s' (no ' at the beginning & end)
    """

    def __init__(self, fd, props=['E']):

        self.fda = fd
        #fs = io2.Folder(fd, 'xyz').fs
        fs = io2.Folder(fd,'sdf').fs
        assert len(fs) > 0, '#ERROR: no SDF file exists'
        self.fs = fs
        idx = []
        for i,f in enumerate(fs):
            lbi = f.split('frag')[1][:-4].split('_')[1]
            _idx = int(lbi)-1
            # one has to subtract 1 (IDX in frag_[IDX].com starts from 1, while python idx starts from 0)
            idx.append(_idx)
        c2amap = np.array(idx,np.int)
        self.c2amap = c2amap # (amon) to (molecule graph) map
        c = np.bincount(idx)
        i = np.nonzero(c)[0]
        ncs = c[i]
        nmt = len(ncs)
        self.nmt = nmt
        self.ncs = ncs
        self.nct = len(fs)
        ics2 = np.cumsum(ncs)
        ics1 = np.array([0]+list(ics2[:-1]))

        #obj = molecules(fs,props=props)
        #self.obj = obj
        #ys = obj.ys
        objsc = []; ys = []; amons = []
        zs=[]; nsheav=[]; nas=[]; coords=[]
        for i,f in enumerate(fs):
            objc = crk.RDMol(f)
            zs += list(objc.zs)
            nas.append(objc.na)
            coords += list(objc.coords)
            nsheav.append(objc.nheav)
            objc.get_angles(wH=F,key='ia')
            objc.get_dihedral_angles(wH=F,key='ia')
            objsc.append(objc)
            ys.append( [ objc.prop[key] for key in props ] )
            if np.any(i==ics1):
                amons.append( objc.prop['smiles_indigo'] )
        self.nas = np.array(nas,np.int)
        self.zs = np.array(zs,np.int)
        self.coords = np.array(coords)
        self.nsheav = np.array(nsheav,np.int)
        self.objsc = objsc
        #ys = np.loadtxt(fd+'/ys.dat') * h2kc
        # assume original unit to be Hartree for energy
        #assert len(ys)==len(fs)

        #fsmi = fd+'/o.smi'
        #if not os.path.exists(fsmi):
        #    print '#ERROR: plz run `tosmiles %s/*c001.xyz >%s/o.smi` first'%(fd,fd)
        #    sys.exit(2)
        #else:
        #    amons = [ si.strip().split()[-1] for si in file(fsmi).readlines() ]
        assert len(amons)==len(ncs)
        self.a2cmap = [range(ics1[iamon],ics2[iamon]) for iamon in range(nmt)]
        self.ics1 = ics1
        self.ics2 = ics2
        self.amons = amons
        self.props = props
        self.ys = np.array(ys)


def get_angles34(_dic, iass):
    _keys = _dic.keys()
    types = []; vals = []
    for _ias in iass:
        _idxr = np.arange( len(_ias) )
        _amap = dict(zip(_ias,_idxr))
        _set = set(_ias)
        dic2 = {}
        for _key in _keys:
            if set(_key) <= _set:
                key2 = tuple([ _amap[_] for _ in _key ])
                if key2[0] > key2[-1]:
                    key2 = tuple([ _amap[_] for _ in _key[::-1] ])
                dic2[key2] = _dic[_key]
        newkeys = dic2.keys()
        newkeys.sort()
        types.append( newkeys )
        vals.append( np.array([dic2[newkey] for newkey in newkeys],dtype=int) )
    return types, vals


class AmonQ(object):
    """ Get amons for query mol """
    def __init__(self, dt):
        assert type(dt) is AmonDict
        self.props = dt.props
        self.zs_a = dt.zs
        self.nas_a = dt.nas
        self.coords_a = dt.coords
        self.nsheav_a = dt.nsheav
        self.ics1 = dt.ics1
        self.ics2 = dt.ics2
        self.ias2 = np.cumsum(dt.nas)
        self.ias1 = np.array([0]+list(self.ias2[:-1]))
        self.amons = dt.amons
        self.objsc = dt.objsc
        self.nct = dt.nct
        self.ys_a = dt.ys
        self.c2amap = dt.c2amap
        self.a2cmap = dt.a2cmap
        self.fs = np.array(dt.fs)
        self.nmt = dt.nmt
        self.fmt = '%%0%dd'%(len(str(dt.nmt)))
        self.fda = dt.fda

    def query(self, f, k=7):
        self.fq = f
        #zs, coords, ydic = read_xyz_simple(f,opt='z')
        objq = crk.RDMol(f)
        zs, coords, ydic = objq.zs, objq.coords, objq.prop
        smi = ydic['smiles_indigo']
        ys_q = np.array([ [ydic[key] for key in self.props ] ])
        nheav = (np.array(zs)>1).sum()
        #if smi is None:
        #    # perceive amons first
        #    #try:
        #    o = Mol(zs, coords, ican=True)
        #    smi = o.can
        #    #except:
        #    #raise '#ERROR: failed to perceive SMILES'
        #
        # default: use indigo to gen amons
        ao = cma.amon(smi, k) # amon object
        assert ao.iok
        amons_q, ns_q, ats_q = ao.get_amons(iao=T) # idxs of atoms (in query mol) as output as well
        self.amons_q = amons_q
        self.ns_q = ns_q
        self.ys_q = ys_q
        self.zs_q = np.array(zs,np.int)
        self.nsheav_q = [nheav]
        self.coords_q = np.array(coords)
        self.ats_q = ats_q
        # tor
        #self.x_q = get_torsions( atoms(zs,coords) ) #; sys.exit()
        objq.get_angles(wH=F,key='ia')
        objq.get_dihedral_angles(wH=F,key='ia')
        self.objq = objq
        # slatm
        #xobj = sl.slatm([len(zs)],zs,coords)
        #xobj.get_x(rb=F, mbtypes=None, param={'racut':4.8})
        #self.x_q = xobj.xsa
        #self.mbtypes = xobj.mbtypes

    def is_gcm_close(self,iaq,conf):
        """ check if the given conformer `conf is close to some
        local structure in the query mol by distribution of torsions!!

        vars
        ==============
        iaq : idx of amon generated on-the-fly for query mol
        conf: conformer in the dictionary
        rmsd: ...
        """
        param = self.param
        iok = T
        n = self.ns_q[iaq]
        msr = [] # local structures to be compared with input `conf
        for i in range(n):
            ias = self.ats_q[iaq][i]
            zsi = self.zs_q[ias]
            psi = self.coords_q[ias]
            mi = atoms(zsi[zsi>1], psi[zsi>1])
            msr.append(mi)
        ms = msr + [conf]#; print '  ns = ', [ _m.na for _m in ms ]
        ds = cdist(ms, param)[-1]; #print '  ds = ', ds[-1,:-1]
        return np.any(ds[-1,:-1]<=param['thresh'][0])

    def is_aslatm_close(self,conf):
        """ check if all local atomic env in conf is close to
        some in the query (based on aSLATM)
        """
        param = self.param
        iok = T
        xobj = sl.slatm([len(conf.zs)],conf.zs, conf.coords)
        xobj.get_x(rb=F, mbtypes=self.mbtypes, param={'racut':4.8})
        xc = xobj.xsa
        ds = qd.l2_distance(xc,self.x_q)# xc)
        return np.any(ds <= param['thresh'][0])

    def amons_filter(self,smile,cs,thresh=[36.,45.]):
        """ filter the given conformers (`cs)
        so as to find the ones that are valid amons of query
        """
        nc = len(cs)
        ics = np.arange(nc)
        patt = crk.smi2patt(smile)
        _ = Chem.MolFromSmarts(patt)
        matches_q = self.objq.m.GetSubstructMatches(_)
        #print ' matches_q = ', matches_q
        nmatch = len(matches_q)
        types3_q, angs_q = get_angles34(self.objq.angs, matches_q)
        types4_q, dangs_q = get_angles34(self.objq.dangs, matches_q)
        icsc = [] # chosen ics
        for i,objc in enumerate(cs):
            #print ' -- i = ', i
            matches_c = objc.m.GetSubstructMatches(_)
            types3_c, angs_c = get_angles34(objc.angs, matches_c)
            types4_c, dangs_c = get_angles34(objc.dangs, matches_c)
            #dic3_c = dict(zip(types3_c[0],angs_c[0]))
            #dic4_c = dict(zip(types4_c[0],dangs_c[0]))
            # note that angs & torsions found in amons and query may differ, e.g.,
            # amon=CCCC, query=C1CCC1C
            #assert set(types3_c[0])<=set(types3_q[0]), '#ERROR: angle types differ'
            #assert set(types4_c[0])<=set(types4_q[0]), '#ERROR: tor types differ'
            iok = False
            for j in range(nmatch):
                _a = angs_q[j]; dic3_q = dict(zip(types3_q[j],_a))
                _d = dangs_q[j]; dic4_q = dict(zip(types4_q[j],_d))
                _angs_q = np.array([ dic3_q[t3] for t3 in types3_c[0] ])
                _dangs_q = np.array([ dic4_q[t4] for t4 in types4_c[0] ])
                dfs3 = angs_c[0] - _angs_q
                dfs4 = dangs_c[0] - _dangs_q
                iok1 = np.all(np.abs(dfs3)<=thresh[0])
                iok2 = np.all(np.abs(dfs4)<=thresh[1])
                #if not iok1: print ' ang mismatch', list(angs_c[0]),list(_angs_q)
                #if not iok2: print ' tor mismatch', list(dangs_c[0]),list(_dangs_q)
                if iok1 and iok2:
                    iok = True
                    break
            if not iok: continue
            icsc.append(i)
        return icsc

    def is_torsion_close(self,conf,fi,thresh):
        """ check if dihedral angles are close """
        xc = get_torsions(conf)
        iok = T
        keys = xc.keys()
        for key in keys:
            vsc = xc[key]; msg = '%s does not exist?'%key
            if key not in self.x_q.keys():
                print msg, ' Related file is ', fi
                so = '%d\n\n'%(len(conf.zs))
                for iz,zi in enumerate(conf.zs):
                    so += '%2d %.4f %.4f %.4f\n'%(zi,conf.coords[iz][0],conf.coords[iz][1],conf.coords[iz][2])
                print so
            vsq = np.array(self.x_q[key])
            #ioks = np.any( np.abs(vsc[...,np.newaxis]-vsq[np.newaxis,...])<=thresh, axis=1 )
            if np.any([ np.all(np.abs(vsq-vc)>thresh) for vc in vsc ]):
                iok = F
                break
        return iok

    def get_cids(self,param={'M':'cml1','rp':1.0,'wz':F,'thresh':[0.1]},diagnose=F):
        """ get amon idxs """
        self.param = param
        _aids = []; _cids = []; _ias = []
        # first get all conformers
        aids0 = []; cids0 = []; ats0 = []
        for iaq, aq in enumerate(self.amons_q):
            iad = self.amons.index(aq) # idx of amon in the AmonDict
            aids0.append( iad )
            cids0 += self.a2cmap[iad]
            ats0.append( self.ats_q[iaq] )
        if diagnose:
            _fd = 'indiv/'+self.fq.split('frag')[1][:-4].split('_')[1]
            _fd2 = _fd + '/neglected'
            if not os.path.exists(_fd): os.system('mkdir -p %s'%_fd)
        if param['thresh'][0] > 0: # negative value of `thresh indicates using all amons
            if param['M'] in ['cm','cml1']:
                for cid in cids0:
                    _aid = self.c2amap[cid]
                    _iasc = np.arange(self.ias1[cid],self.ias2[cid])
                    zsi = self.zs_a[_iasc]
                    iasc = _iasc[ self.zs_a[_iasc] > 1 ]
                    nai = len(iasc)
                    if nai >= 4:
                        ci = atoms(self.zs_a[iasc],self.coords_a[iasc])
                        if not self.is_gcm_close(_aid,ci):
                            continue
                    if _aid not in _aids: _aids.append(_aid)
                    _cids.append(cid)
                    _ias += list(_iasc)
            elif param['M'] in ['tor','torsion']:
                assert len(param['thresh']) == 2, '#ERROR: len(thresh)!=2'
                #for cid in cids0:
                #    _aid = self.c2amap[cid]
                #    _iasc = np.arange(self.ias1[cid],self.ias2[cid])
                #    ci = atoms(self.zs_a[_iasc],self.coords_a[_iasc])
                #    fi = self.fs[cid]
                #    if len(_iasc[ self.zs_a[_iasc] > 1 ]) >= 4:
                #        if not self.is_torsion_close(ci,fi,param['thresh']):
                #            continue
                #    if _aid not in _aids: _aids.append(_aid)
                #    _cids.append(cid)
                #    _ias += list(_iasc)
                for _aid in aids0:
                    cids = np.array(self.a2cmap[_aid],dtype=int)
                    _ = cids[0]
                    zsi = self.zs_a[ self.ias1[_]:self.ias2[_] ]
                    if len(zsi[zsi>1]) >= 4:
                        smi = self.amons[_aid]
                        csi = [self.objsc[ic] for ic in cids] # `cs: conformers !Todo
                        #print "aid=", _aid
                        #print "fs = ['%s']"%( "','".join( self.fs[cids] ) )
                        ccidsr = self.amons_filter(smi,csi,param['thresh'])
                        if len(ccidsr) == 0:
                            #print ' [no conformer chosen] f0=%s'%(self.fs[cids[0]])
                            fs_noconf = self.fs[cids]
                            if diagnose and len(fs_noconf) > 0:
                                if not os.path.exists(_fd2): os.system('mkdir -p %s'%_fd2)
                                #print ' files that are totally ignored are now in %s'%_fd2
                                os.system('cp %s %s'%(' '.join(fs_noconf), _fd2))
                            continue
                        else:
                            fs_conf = self.fs[cids[ccidsr]]
                            if diagnose and len(fs_conf) > 0:
                                os.system('cp %s %s'%(' '.join(fs_conf), _fd))
                    else:
                        ccidsr = range(len(cids))
                        fs_conf = self.fs[cids[ccidsr]]
                        if diagnose and len(fs_conf) > 0:                  
                            os.system('cp %s %s'%(' '.join(fs_conf), _fd))
                    if _aid not in _aids: _aids.append(_aid)
                    ccids = cids[ccidsr]
                    _cids += list(ccids)
                    for _cid in ccids:
                        # now add all atoms in these conformers to `_ias
                        _ias += range(self.ias1[_cid],self.ias2[_cid])
            else:
                raise '#ERROR: unknown `M'
        else:
            _cids = cids0
            _aids = aids0
            _ias = []
            for cid in cids0:
                _iasc = np.arange(self.ias1[cid],self.ias2[cid])
                _ias += list(_iasc)
        if not set(_aids)<=set(aids0):
            print ' #ERROR: set(_aids)<=set(aids0) does not hold true!!'
        aids_r = np.setdiff1d(aids0,_aids)
        #if diagnose:
        #    _fd = 'indiv/'+self.fq.split('frag')[1][:-4].split('_')[1]
        #    if not os.path.exists(_fd): os.system('mkdir -p %s'%_fd)
        #    sf = ' '.join([ self.fda+'/frag_'+self.fmt%(ir+1)+'_*.sdf' for ir in aids0])
        #    os.system( 'cp %s %s'%(sf,_fd) )
        print '  #### amons ignored: ', ''.join(['%d: %s, '%(ir+1,self.amons[ir]) for ir in aids_r])
        print '  #### amons used: ', ''.join(['%d: %s, '%(ir+1,self.amons[ir]) for ir in _aids])
        print '  #### total num of conformers selected: %d out of %d'%(len(_cids),len(cids0))
        self.aids = _aids
        self.cids = _cids
        self.ias = _ias

    def wrapup(self):
        # now merge training amons & query molecule
        self.nas = np.array( list(self.nas_a[self.cids]) + [len(self.zs_q)], np.int)
        self.zs = np.concatenate((self.zs_a[self.ias], self.zs_q)).astype(np.int)
        self.coords = np.concatenate((self.coords_a[self.ias], self.coords_q))
        self.nsheav = np.array(list(self.nsheav_a[self.cids])+self.nsheav_q, np.int)
        _ys = np.concatenate( (self.ys_a[self.cids], self.ys_q), axis=0)
        if _ys.shape[1] == 1:
            self.ys = _ys[:,0]
        else:
            self.ys = _ys


def diagnose1(fa,fq):
    """ do diagnose for one amon and the query """
    oa = crk.RDMol(fa)
    oa.get_angles(wH=F,key='ia')
    oa.get_dihedral_angles(wH=F,key='ia')
    oq = crk.RDMol(fq)
    oq.get_angles(wH=F,key='ia')
    oq.get_dihedral_angles(wH=F,key='ia')
    smi = oa.prop['smiles_indigo']
    patt = crk.smi2patt(smi)
    print 'patt=',patt
    mp = Chem.MolFromSmarts(patt)
    iass_a = oa.m.GetSubstructMatches(mp)
    iass_q = oq.m.GetSubstructMatches(mp)

    types_a, vals_a = get_angles34(oa.dangs, iass_a)
    #dic_a = dict(zip(types_a[0],vals_a[0]))
    types_q, _vals_q = get_angles34(oq.dangs, iass_q)
    n = len(types_q)
    vals_q = []
    for i in range(n):
        types = types_q[i]; vals = _vals_q[i]; #print ' -- types = ', types
        dic_q = dict(zip(types,vals))
        _vals = []
        for key in types_a[0]:
            _vals.append(dic_q[key])
        vals_q.append(_vals)
    print ' amon:'
    print types_a[0]
    print list(vals_a[0])
    print ' query:'
    for i in range(n): print vals_q[i]
    print ' Difference:'
    for i in range(n): print list( vals_q[i]-vals_a[0])


def calc_rmsd(mol, mode='RMSD'):
    """
    calculate conformer-conformer RMSD.
    """
    if mode == "TFD":
        ds = TorsionFingerprints.GetTFDMatrix(mol)
    else:
        nc = mol.GetNumConformers()
        ds = np.zeros((nc,nc),dtype=float)
        cs = mol.GetConformers()
        for i, ci in enumerate(cs):
            ic = ci.GetId()
            for j, cj in enumerate(cs):
                if i >= j: continue
                jc = cj.GetId()
                ds[i,j] = ds[j,i] = AllChem.GetBestRMS(mol, mol, ic, jc)
    return ds


def reset(_m):
    m = copy.deepcopy(_m)
    na = m.GetNumAtoms()
    for bi in m.GetBonds():
        i, j = bi.GetBeginAtomIdx(), bi.GetEndAtomIdx()
        bi.SetBondType( bo2bt['1.0'] ) #Chem.BondType.SINGLE )
    return m

def unset(_m, bom):
    m = copy.deepcopy(_m)
    for bi in m.GetBonds():
        i, j = bi.GetBeginAtomIdx(), bi.GetEndAtomIdx()
        bi.SetBondType( bo2bt['%.1f'%bom[i,j]] )
    return m



def find_number_of_unique_set(s):
    """
    E.g., for a list of sets
      s = [ {1,2,3}, {2,3,4}, {2,3,4,5}, {1,2,3,4}]
    The corresponding unique set is [ {1,2,3}, {2,3,4} ]
    """
    ns = [ len(si) for si in s ]
    n = len(s)
    iss = np.arange(n)
    idx = np.argsort(ns)
    so = []
    for i in idx:
        si = s[i]
        if i==0:
            so.append(si)
        else:
            _idx = np.setdiff1d(iss,[i])
            iadd = T
            for j in _idx:
                if si.issuperset(s[j]):
                    iadd = F
                    break
            if iadd: so.append(si)
    return len(so)


class EmbedMol(object):

    def __init__(self, mol):
        self.mol = mol
        self.bom = crk.get_bom(mol)
        self.cns = (self.bom > 0).sum(axis=0)
        zs = np.array([ ai.GetAtomicNum() for ai in mol.GetAtoms()])
        na = len(zs); ias = np.arange(na)
        self.na = na
        self.ias = ias
        self.zs = zs
        self.ias_heav = ias[zs>1]
        self.optg0 = F # is ff optg done?

    def estimate_nc(self):
        """ a coarse estimation of max number of conformers is 3^n
        where n is the number of quantified rotatable bonds
        A better method uses bond orders.
        Note that not all rotatable bonds are quantified, e.g.,
        for H3C-CRR'R'', H2N-CRR'R'', H2C=CRR'
        R1       R3
          \     /
           C = C      C=C contributes 2
          /     \
        R2       R4
        """
        mol = self.mol
        torsma = '[!$(*#*)&!D1]~[!$(*#*)&!D1]'
        q = Chem.MolFromSmarts(torsma)
        matches = mol.GetSubstructMatches(q)
        nmat = len(matches)
        #torsions = []

        atsr = get_ring_nodes(mol,3,7,F) # since mostly the molecules concerned here are amons with N_I <=7
        #print ' -- atsr = ', atsr
        inrs = np.zeros(self.na, dtype=int) # [this atom is] in [how many] number of rings
        for ia in self.ias_heav:
            _sets = []
            for _ats in atsr:
                if ia in _ats:
                    _sets.append(_ats)
            #print ' -- ia, _sets = ', ia, _sets
            inr = find_number_of_unique_set(_sets)
            inrs[ia] = inr
        #print ' -- inrs = ', inrs
        if nmat == 0:
            ns = [1]
            print '    |__ ns = ', ns
            nc = 1
            self.nc = nc
        else:
            ns = []; patts = []
            scale = 0
            for match in matches:
                j = match[0]
                k = match[1]
                cb = set([j,k])
                bond = mol.GetBondBetweenAtoms(j, k)
                aj = mol.GetAtomWithIdx(j)
                ak = mol.GetAtomWithIdx(k)
                hj, hk = [ _hyb[_a.GetHybridization()] for _a in [aj,ak] ]
                iok1 = (hj != 2); iok2 = (hj != 3)
                iok3 = (hk != 2); iok4 = (hk != 3)
                if (iok1 and iok2) or (iok3 and iok4): continue

                # do not allow internal rotation about two adjacent sp2 atoms are in a ring
                if inrs[j] and inrs[k] and hj==2 and hk==2: continue

                pjk = []
                jk = [j,k]
                hsjk = [hj,hk]
                for _ in range(2):
                    ia1 = jk[_]
                    ia2 = j if ia1==k else k
                    hyb = hsjk[_]
                    nbrs = np.setdiff1d(self.ias[self.bom[ia1]>0], [ia2])
                    ihs = (self.zs[nbrs]==1)
                    if np.all(ihs):  # case 'a', e.g., 'a1','a2','a3'
                        # check ~X-CH3, ~X-NH2, ...
                        nh = len(ihs)
                        if hyb==3:
                            # for rotor X-C in ~X-CH3, one torsion is allowed
                            sn = {1:'a3', 2:'a2', 3:'a1'}[nh]
                        else: # hyb==2
                            sn = {1:'a2', 2:'a1', 3:'a1'}[nh]
                    else: # case 'b', e.g., 'b1','b2','b3'
                        inr = inrs[ia1]
                        if self.cns[ia1]==2 and inr: # e.g., O<, S<, Se<,
                            sn = 1
                        else:
                            if hyb==3:
                                sn = {0:'b3', 1:'b3', 2:'b2', 3:'b1', 4:'b1'}[inr]
                            else: # hyb==2:
                                sn = {0:'b2', 1:'b1', 2:'b1', 3:'b1'}[inr]
                    _patt = '%d%s'%(hyb,sn)
                    pjk.append(_patt)
                #print 'j,k = ', j,k, ', pjk = ', pjk
                nci = min([ int(patt[-1]) for patt in pjk ]) # ndic[patt]; sci = scdic[patt]
                if nci > 1:
                    ns.append( nci )
                    if not np.any([inrs[j],inrs[k]]):
                        scale += 1
            if scale == 0: scale = 1
            nc = np.int(np.floor(np.product(ns))) * scale #* 2
            self.nc = nc if nc > 99 else 99
            print '    |__ ns = ', ns
            print '    |__ scale = %d, nc = %d'%(scale, nc)
            self.ns = np.array(ns, np.int)

    def gen_conformers(self, nc=None, nthread=1, maxiter=1200, pruneRmsThresh=-1):
        """ generate conformers """
        # trick rdkit by setting a bond order of 1 for all bonds
        mol = reset(self.mol)
        if nc is None:
            self.estimate_nc()
            nc = self.nc
            print '    |__ estimated num of conformers: ', nc
        #
        # ETKDG method becomes the default since version RDKit_2018
        # (currently we r working with RDKit_2017_09_1, ETKDG has to be manually turned on)
        params = AllChem.ETKDG()
        #params = AllChem.EmbedParameters() # default in RDKit version <= 2017, no ETKDG is used
        #
        #params.maxIterations = maxiter
        params.useBasicKnowledge = T #F
        #params.numThreads = nthread
        #params.pruneRmsThresh = pruneRmsThresh # -1
        params.useExpTorsionAnglePrefs = F #T
        params.onlyHeavyAtomsForRMS = F
        cids = AllChem.EmbedMultipleConfs(mol, nc, params)
        #cids = AllChem.EmbedMultipleConfs(mol, numConfs=nc, maxAttempts=1000, \
        #             pruneRmsThresh=pruneRmsThresh, useExpTorsionAnglePrefs=T, \
        #             useBasicKnowledge=T, enforceChirality=T, numThreads=nthread)
        self.cids = np.array(list(cids),np.int)
        istat = T
        if len(self.cids) == 0:
            # For smi="C1OC2CCC21", no conformer was found when i=0; while it has only 1 conformer, ...
            #print '#ERROR: RDKit failed to find any conformer!'
            istat = F
        self.istat = istat
        # now restore
        self.mol = unset(mol, self.bom)
        self.nconf = len(self.cids)

    @staticmethod
    def get_rmsd(mode='RMSD'):
        """
        calculate conformer-conformer RMSD.
        """
        return calc_rmsd(self.mol, mode=mode)

    def optg(self, ff='mmff94', n=1000):
        """ optimize geometries of all conformers """
        immff = F
        if ff in ['mmff94']:
            props = AllChem.MMFFGetMoleculeProperties(self.mol)
            immff = T
        angs = crk._get_angles_csp2(self.mol)
        self.es = []
        if not hasattr(self,'cids'):
            cids = [-1]
        else:
            cids = self.cids
        for cid in cids:
            for cycle in [0,1]:
                """
                minization is split into 2 parts
                a) The first part tries to correct some ill local geometries in conformers,
                   realized through constraints in angles and will be iterated for maximally
                   200 steps;
                b) Normal geometry minization without constraints, number of iterations: `n-200
                """
                if immff:
                    ff = AllChem.MMFFGetMoleculeForceField(self.mol, props, confId=cid)
                else:
                    ff = AllChem.UFFGetMoleculeForceField(self.mol, confId=cid)
                ff.Initialize()
                if cycle == 0:
                    _n = 200
                    ## The two lines below are essential to obtain reasonable conformer geometries
                    ## If not present, then conformer with some angle centered on sp2-C may be ~90 degrees
                    for i,j,k in angs:
                        ff.MMFFAddAngleConstraint(i,j,k, F, 95, 145, 9999.) # relative=False
                    ## Here, in essense, we manually constrain such angles to be within the range of [95,145] degree
                else:
                    _n = n - 200
                if n > 0:
                    converged = ff.Minimize(maxIts=_n, forceTol=0.0001, energyTol=1e-05)
                #RETURNS: 0 if the optimization converged, 1 if more iterations are required.
            self.es.append( ff.CalcEnergy() )
        #res = AllChem.MMFFOptimizeMoleculeConfs(self.mol, numThreads=1, maxIters=n)
        self.optg0 = T

    def optg2(self, label=None):
        # use MOPAC/PM7 for further optimization
        es = []
        assert self.optg0, '#ERROR: Plz call `optg() first'
        for cid in range(self.nconf):
            c1 = self.mol.GetConformer(cid)
            m1 = self.get_atoms([cid])[0]
            s = 'PM7 PRECISE' # 'BFGS'
            s += '\nTitle: ASE\n\n'
            # Write coordinates:
            for ia in range(self.na):
                symbol = m1.symbols[ia]
                xyz = m1.coords[ia]
                s += ' {0:2} {1} 1 {2} 1 {3} 1\n'.format(symbol, *xyz)
            if label is None:
                label = tpf.NamedTemporaryFile(dir='/tmp').name
            with open(label+'.mop','w') as fid: fid.write(s)
            try:
                exe = os.environ['MOPAC']
            except:
                raise '#ERROR: Plz do `export MOPAC=/path/to/MOPAC/executable'
            iok = os.system( '%s %s.mop 2>/dev/null'%(exe, label) )
            if iok > 0:
                raise '#ERROR: MOPAC failed !!'

            # get energy
            opf = label+'.out'; print '    |__ opf = ', opf
            cmd = "grep 'FINAL HEAT' %s | tail -n 1 | awk '{print $6}'"%opf
            e = eval( io2.cmdout2(cmd) ) # Heat of formation [kcal/mol]
            print '    |____ e = ', e; es.append( e )

            # get coords
            cmd = "sed -n '/                             CARTESIAN COORDINATES/,/Empirical Formula:/p' %s"%opf
            conts = io2.cmdout2(cmd).split('\n')[2:-3]
            _coordsU = []
            for k in range(self.na):
                tag, symb, px, py, pz = conts[k].strip().split()
                _coordsU.append( [px,py,pz] )
            coordsU = np.array(_coordsU, dtype=float)
            for i in range(self.na):
                pi = Point3D()
                pi.x, pi.y, pi.z = coordsU[i]
                c1.SetAtomPosition(i, pi)

            #if not os.path.exists('../trash'): os.system('mkdir ../trash')
            #iok = os.system('mv %s.arc %s.mop %s.out ../trash/'%(label,label,label))
        self.es = es


    def prune_conformers(self, param={'M':'cml1', 'rp':1.0,'thresh':0.25,'wz':F,'sort':T}):
        """
        get the chosen conformer ids, i.e., the unique conformers

        vars
        ======================
        param['M']: 'cm','cml1'

        Notes
        ======================
        a) deficiency of 'rmsd': cannot distinguish two distinct conformers of OC=O,

            O                  O
            ||                 ||
            C     H            C
           / \  /             / \
          H   O              H   O
                                 |
                                 H
                RMSD = 0.003 (UFF optimized geom)
          While with 'cml1+sort': dcm = 0.163 if wz=F else 1.311
        b) coveat of 'cm': failure to distinguish conformers of CH3-CH3
        """
        if param['M'] in ['rmsd']:
            ds = self.get_rmsd()
        elif param['M'] in ['cm','cml1']:
            ds = self.get_dcm(param)
        else:
            raise '#ERROR: unknow rep'
        #print ' ++ ds = ', ds
        #print '   |__ es = ', np.array(self.es)
        seq = np.argsort(self.es)  # sort by increasing energy
        ccids = []
        for i in seq:
          # always keep lowest-energy conformer
          if len(ccids) == 0:
            ccids.append(i)
            continue

          # discard conformers within the RMSD threshold
          if np.all(ds[i][ccids] >= thresh):
            ccids.append(i)
        self.nconf = len(ccids)
        # creat a new mol object with unique conformers
    	new = Chem.Mol(self.mol)
        new.RemoveAllConformers()
        for i in ccids:
            ci = self.mol.GetConformer(i)
            new.AddConformer(ci, assignId=True)
        self.mol = new

    def write_conformers(self, filename): # ccids):
        """ write conformers to sdf files """
        cnt = 0
        for confId in range(self.nconf): #ccids:
            w = Chem.SDWriter('%s_c%03d.sdf'%(filename,cnt+1))
            w.write(self.mol, confId=confId)
            w.flush()
            w.close()
            cnt += 1

    def write(self, f):
        Chem.MolToMolFile(self.mol, f)

    def get_atoms(self, cids=None):
        na = self.na
        zs = self.zs
        if cids == None:
            cids = range(self.nconf)
        ms = []
        for cid in cids:
            ps = []
            c = self.mol.GetConformer(cid)
            for ia in range(na):
                psi = c.GetAtomPosition(ia)
                ps.append( [psi.x, psi.y, psi.z] )
            ms.append( atoms(zs,ps) )
        return ms

    def get_dcm(self, param):
        objs = self.get_atoms()
        return cdist(objs, param)[-1]


if __name__ == "__main__":
    """
    generate conformers for a input molecule

    Attention: most frequently, the input are sdf files of AMONs !!!!!!!!!!
    """
    import stropr as so

    _args = sys.argv[1:]
    if ('-h' in _args) or (len(_args) < 3):
        print "Usage: "
        print "   genconf [-rms 0.1] [-thresh 0.1] [-nthread 1] [-fd folder_name] [q1.sdf q2.sdf ...]"
        print "   genconf [-rms 0.1] [-thresh 0.1] [-nthread 1] [-fd folder_name]  01.smi"
        sys.exit()

    print ' \n Now executing '
    print '         genconf ' + ' '.join(sys.argv[1:]) + '\n'

    idx = 0
    keys=['-fd','-ofd']; hask,fd,idx = so.parser(_args,keys,'amons/',idx)
    if not os.path.exists(fd): os.system('mkdir %s'%fd)

    keys=['-rms']; hask,_rms,idx = so.parser(_args,keys,'-1',idx,F); rms=eval(_rms)
    keys=['-thresh']; hask,_thresh,idx = so.parser(_args,keys,'0.25',idx,F); thresh=eval(_thresh)
    keys=['-nthread']; hask,_nthread,idx = so.parser(_args,keys,'1',idx,F); nthread=int(_nthread)
    keys=['-nstep']; hask,_nstep,idx = so.parser(_args,keys,'999',idx,F); nstep=int(_nstep)
    keys=['-ow','-overwrite']; ow,idx = so.haskey(_args,keys,idx)
    keys=['-allow_smi']; allow_smi,idx = so.haskey(_args,keys,idx)
    keys=['-optg2','-pm7','-mopac']; optg2,idx = so.haskey(_args,keys,idx)
    keys=['-nc','-nconf']; hask,snc,idx = so.parser(_args,keys,'None',idx,F); nc = eval(snc)
    keys=['-ff',]; hask,ff,idx = so.parser(_args,keys,'mmff94',idx,F)

    keys=['-nj','-njob']; hask,snj,idx = so.parser(_args,keys,'1',idx,F); nj = int(snj)
    if nj > 1:
        keys=['-i','-id']; hask,sid,idx = so.parser(_args,keys,None,idx)
        assert hask, '#ERROR: need to set -i [INT_VAL]'
        id = int(sid)
    else:
        hasID,idx = so.haskey(_args,keys,idx)
        assert not hasID

    args = _args[idx:]
    narg = len(args)
    ms = []; fs = []; lbs = []; smiles=[]
    isdf = T; fs0 = []
    for arg in args:
        assert os.path.exists(arg), '#ERROR: input should be file, of format *.smi or *.sdf'
        if arg[-3:] in ['smi','can']:
            isdf = F
            if allow_smi:
                assert narg == 1
                for _si in file(arg).readlines():
                    si = _si.strip().split()[0]; #print ' +++++ si = ', si
                    if si != '':
                        #print '    ** smi = ', si
                        _mi = Chem.MolFromSmiles(si)
                        mi = Chem.AddHs(_mi)
                        ms.append(mi); lbs.append(si); smiles.append(si)
                nm = len(ms)
                fmt = '%s/frag_%%0%dd'%(fd, len(str(nm)) )
                fs = [ fmt%i for i in range(1,nm+1) ]
            else:
                print '''
 #############################################################
 RDKit has problem processing some molecule with high strain
 (i.e., multiple small-membered rings). For 100% success of
 conformer generation, plz use as input SDF files produced
 by openbabel (from SMILES strings).

    obabel amons.smi -O frag_.sdf -m --gen3d slowest'
    /usr/bin/rename -- 's/(\w+)_(\d+)/sprintf("%%s_%%03d",$1,$2)/e' frag_*sdf
    for f in frag_*sdf
    do
        base=${f%%.sdf}
        obminimize -ff uff -cg -c 1e-4 $f >$base.pdb
    done
    obabel frag_*sdf -osdf -m

 Or you can use an existing shell script called `obgenc1 and
 the usage is:
    obgenc1 amons.smi


 At last, if you insist on using SMILES file as input, turn on -allow_smi

    genconf -thresh 0.1 -fd amons -allow_smi amons.smi

#############################################################
'''
                sys.exit(2)
        elif os.path.isdir(arg): #arg[-3:] in ['sdf','mol']:
            _fs = io2.Folder(arg,'sdf').fs # assume ground state geometries of amons are provided as sdf files
            if len(_fs) == 0:
                print '#ERROR: no sdf file (for ground state geometries of amons) was found!'
                print '        Try to run `optg -ff mmff99 input.smi` to generate these sdf files'
                sys.exit(2)
            fs0 += _fs
            for f in _fs:
                fs.append(fd+'/'+f[:-4].split('/')[-1])
                m1 = Chem.MolFromMolFile(f,removeHs=F)
                m1c = Chem.MolFromMolFile(f)
                smiles.append( Chem.MolToSmiles(m1c) )
                ms.append(m1); lbs.append('')
        else:
            raise '#ERROR: input format not allowed'

    nm = len(ms)
    if nj > 1:
        nav = nm/nj + 1 if nm%nj > 0 else nm/nj
        i1 = id*nav; i2 = (id+1)*nav
        if i2 > nm: i2 = nm
    else:
        i1 = 0; i2 = nm

    for i in range(i1,i2):
        _fn = fs[i]
        _lb = lbs[i]
        m = ms[i]
        _smi = smiles[i]
        print " #Molecule %30s %20s %s"%(_fn,_lb,_smi)
        nheav = m.GetNumHeavyAtoms()
        #s1,s2 = _fn.split('_')
        fn = _fn #'%sNI%d_%s'%(s1,nheav,s2)
        _ofs = []
        if os.path.exists('%s_c001.sdf'%fn):
            _ofs = io2.cmdout('ls %s_c???.sdf'%fn)
        if len(_ofs) > 0 and (not ow): continue
        obj = EmbedMol(m)
        obj.gen_conformers(nc=nc,pruneRmsThresh=rms,nthread=nthread)
        if not obj.istat:
            assert isdf, '#ERROR: __only__ sdf is allowed in this case'
            print '#ERROR: when RDKit failed to generate any conformer for %s %s.sdf %s'%(_smi,fs0[i])
            iok = os.system('cp %s %s_c001.sdf'%(fs0[i], _fn))
            continue
        obj.optg(n=nstep, ff=ff) # somehow, setting ff to 'mmff94' may result in 1 conformer for many mols, e.g., CCC=N
        param = {'M':'cml1','rp':1,'wz':F,'thresh':thresh}
        obj.prune_conformers(param=param)
        print "    |__ %4d conformers generated"%( obj.nconf )
        if optg2: # further optg by MOPAC
            obj.optg2()
            obj.prune_conformers(param=param)
            print "    |__ %4d refined conformers (by MOPAC)"%( obj.nconf )
        obj.write_conformers(fn) # ccids)
        #else:
        #    assert len(smiles)>0, '#ERROR: how comes?'
        #    obm = cob.Mol(_smiles[i],addh=T,make3d=T,ff='uff',steps=900)
        #    obm.write(fn+'_c001.sdf')
        #    print "    |__ OpenBabael was used to generate structure!"
