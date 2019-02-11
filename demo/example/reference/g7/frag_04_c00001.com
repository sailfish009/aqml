%pal nprocs 2 end
%maxcore 1000
! b3lyp TIGHTSCF
! cc-pvdz def2/J RIJCOSX
! Opt

%geom
maxiter 60
TolE 1e-4
TolRMSG 2e-3
TolMaxG 3e-3
TolRMSD 2e-2
TolMaxD 3e-2
end

*xyz 0 1
C 0.3147 -0.9508 0.5131
C -0.142 0.2971 0.3525
C -1.5367 0.7268 0.6695
H -0.3184 -1.7463 0.8937
H -2.1469 -0.0983 1.0503
H -1.5203 1.517 1.4263
H 1.3411 -1.2027 0.2644
H 0.5294 1.0617 -0.0322
H -2.0188 1.1214 -0.2301
*
