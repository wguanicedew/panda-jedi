import sys
import time
import random

from pandajedi.jedicore import Interaction
from pandajedi.jedicore.ThreadUtils import ZombiCleaner


class JediKnight (Interaction.CommandReceiveInterface):
    # constructor
    def __init__(self,commuChannel,taskBufferIF,ddmIF,logger):
        Interaction.CommandReceiveInterface.__init__(self,commuChannel)
        self.taskBufferIF = taskBufferIF
        self.ddmIF        = ddmIF
        self.logger       = logger 
        # start zombi cleaner
        ZombiCleaner().start()


    # start communication channel in a thread
    def start(self):
        # start communication channel
        import threading
        thr = threading.Thread(target=self.startImpl)
        thr.start()
        

    # implementation of start()
    def startImpl(self):
        try:
            Interaction.CommandReceiveInterface.start(self)
        except:
            errtype,errvalue = sys.exc_info()[:2]
            self.logger.error('crashed in JediKnight.startImpl() with %s %s' % (errtype.__name__,errvalue))


    # parse init params
    def parseInit(self,par):
        if isinstance(par,list):
            return par
        try:
            return par.split('|')
        except:
            return [par]


    # sleep to avoid synchronization of loop
    def randomSleep(self,minVal=0,maxVal=30):
        time.sleep(random.randint(minVal,maxVal))


            
# install SCs
Interaction.installSC(JediKnight)
