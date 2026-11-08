"""Read tracked Chart snapshots only; reject symlinks and submodules."""
import subprocess
from v3_delivery_contract import ROOT,CHARTS
from v3_release_bundle import require


def local_charts():
 result={}
 for chart in CHARTS:
  prefix='deploy/v3/'+chart+'/'
  listing=subprocess.check_output(['git','ls-tree','-rz','HEAD','--',prefix],cwd=ROOT).split(b'\0')
  for item in filter(None,listing):
   meta,path=item.split(b'\t',1);mode,kind,_=meta.split();require(mode in [b'100644',b'100755']and kind==b'blob','Only tracked regular Chart files')
   name=path.decode();result['charts/'+chart+'/'+name[len(prefix):]]=subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT)
 return result
