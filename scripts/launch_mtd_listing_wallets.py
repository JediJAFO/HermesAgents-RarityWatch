"""Daily local/no-agent launcher. No collection or notifications on import."""
import os,sys,subprocess,json
from pathlib import Path
HOME=Path(__file__).resolve().parents[1]
def launch_spec():
    return {'args':[sys.executable,str(HOME/'scripts/mtd_listing_wallets.py'),'--run','--seconds','21600'],'cwd':str(HOME/'scripts')}
def main():
    spec=launch_spec()
    with open(HOME/'logs/mtd-listing-wallets-worker.log','ab') as log:
        kwargs={'stdin':subprocess.DEVNULL,'stdout':log,'stderr':log,'cwd':spec['cwd']}
        if os.name=='nt':kwargs['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS
        else:kwargs['start_new_session']=True
        worker=subprocess.Popen(spec['args'],**kwargs)
    print(json.dumps({'status':'launched','pid':worker.pid,'scope':'MTD listing wallets; dashboard only','llm_tokens':0}))
if __name__=='__main__':main()
