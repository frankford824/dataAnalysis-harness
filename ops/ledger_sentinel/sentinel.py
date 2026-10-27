from .executor import Executor
from .probes import Probes
from .state import State, digest, now


class Sentinel:
    def __init__(self,home,config,*,fetch=None,runner=None):
        self.state=State(home);self.config=config
        if config.get('schema_version')!=1:raise ValueError('Unsupported sentinel configuration')
        self.probes=Probes(config.get('probes',[]),fetch)
        kwargs={'runner':runner} if runner else {}
        self.executor=Executor(self.state,self.probes,config.get('actions',[]),**kwargs)

    def once(self):
        interrupted=self.state.reconcile_interrupted()
        results=[];recoveries=[]
        for identifier,spec in self.probes.config.items():
            result=self.state.observe(self.probes.read(identifier));results.append(result)
            action=spec.get('recovery_action')
            if action and result['status'] in spec.get('recovery_states',['failed']):
                # Stable incident identity makes unchanged failures idempotent.
                key=digest([identifier,result['incident_id'],self.config])
                recoveries.append(self.executor.execute(action,'incident-'+key))
        status='ok' if results and not interrupted and all(r['status']=='ok' for r in results) else 'attention'
        return {'status':status,'at':now(),'observations':results,'recoveries':recoveries,
                'interrupted_executions':interrupted,
                'unknown_count':sum(r['status']=='unknown' for r in results),
                'failed_count':sum(r['status']=='failed' for r in results)}
