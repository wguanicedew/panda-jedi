# initialize cx_Oracle using dummy connection
from taskbuffer.Initializer import initializer
initializer.init()

from pandajedi.jedicore import JediTaskBuffer

taskBuffer= JediTaskBuffer.JediTaskBuffer(None)
proxy = taskBuffer.proxyPool.getProxy()
proxy.refreshWorkQueueMap()

#print proxy.workQueueMap.dump()
"""
print proxy.workQueueMap.getQueueWithSelParams('atlas','managed',
                                  prodSourceLabel='managed',
                                  workingGroup='GP_Top')[0].queue_name
print proxy.workQueueMap.getQueueWithSelParams('atlas','managed',
                                  prodSourceLabel='managed',
                                  workingGroup='AP_Top',
                                  processingType='simul')[0].queue_name
print proxy.workQueueMap.getShares('atlas','managed',[1,2,7,8])
"""
