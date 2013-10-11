from pandajedi.jedicore.FactoryBase import FactoryBase
from pandajedi.jediconfig import jedi_config

# logger
from pandacommon.pandalogger.PandaLogger import PandaLogger
logger = PandaLogger().getLogger(__name__.split('.')[-1])


# factory class for task setup
class TaskSetupper (FactoryBase):

    # constructor
    def __init__(self,vo,sourceLabel):
        FactoryBase.__init__(self,vo,sourceLabel,logger,
                             jedi_config.tasksetup.modConfig)


    # main
    def doSetup(self,taskSpec):
        return self.getImpl(taskSpec.vo,taskSpec.prodSourceLabel).doSetup(taskSpec)