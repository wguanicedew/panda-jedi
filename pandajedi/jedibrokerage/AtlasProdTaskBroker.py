import re
import sys
import random
import traceback

from pandajedi.jedicore.MsgWrapper import MsgWrapper
from pandajedi.jedicore import Interaction
from TaskBrokerBase import TaskBrokerBase
from pandajedi.jedicore.ThreadUtils import ListWithLock,ThreadPool,WorkerThread,MapWithLock
import AtlasBrokerUtils
from AtlasProdJobBroker import AtlasProdJobBroker

from pandaserver.userinterface import Client as PandaClient
from pandaserver.dataservice import DataServiceUtils

# cannot use pandaserver.taskbuffer while Client is used
from taskbuffer.JobSpec import JobSpec


# logger
from pandacommon.pandalogger.PandaLogger import PandaLogger
logger = PandaLogger().getLogger(__name__.split('.')[-1])


# brokerage for ATLAS production
class AtlasProdTaskBroker (TaskBrokerBase):

    # constructor
    def __init__(self,taskBufferIF,ddmIF):
        TaskBrokerBase.__init__(self,taskBufferIF,ddmIF)


    # main to check
    def doCheck(self,taskSpecList):
        # make logger
        tmpLog = MsgWrapper(logger)
        tmpLog.debug('start doCheck')
        # return for failure
        retFatal    = self.SC_FATAL,{}
        retTmpError = self.SC_FAILED,{}
        # get list of jediTaskIDs
        taskIdList = []
        taskSpecMap = {}
        for taskSpec in taskSpecList:
            taskIdList.append(taskSpec.jediTaskID)
            taskSpecMap[taskSpec.jediTaskID] = taskSpec
        # check with panda
        tmpLog.debug('check with panda')
        tmpPandaStatus,cloudsInPanda = PandaClient.seeCloudTask(taskIdList)
        if tmpPandaStatus != 0:
            tmpLog.error('failed to see clouds')
            return retTmpError
        # make return map
        retMap = {}
        for tmpTaskID,tmpCoreName in cloudsInPanda.iteritems():
            tmpLog.debug('jediTaskID={0} -> {1}'.format(tmpTaskID,tmpCoreName))
            if not tmpCoreName in ['NULL','',None]:
                taskSpec = taskSpecMap[tmpTaskID]
                if taskSpec.useWorldCloud():
                    # get destinations for WORLD cloud
                    ddmIF = self.ddmIF.getInterface(taskSpec.vo)
                    # get site
                    siteSpec = self.siteMapper.getSite(tmpCoreName)
                    # get nucleus
                    nucleus = siteSpec.pandasite
                    # get output/log datasets
                    tmpStat,tmpDatasetSpecs = self.taskBufferIF.getDatasetsWithJediTaskID_JEDI(tmpTaskID,['output','log'])
                    # get destinations
                    retMap[tmpTaskID] = {'datasets':[],'nucleus':nucleus}
                    for datasetSpec in tmpDatasetSpecs:
                        # skip distributed datasets
                        if DataServiceUtils.getDistributedDestination(datasetSpec.storageToken) != None:
                            continue
                        # get token
                        token = ddmIF.convertTokenToEndpoint(siteSpec.ddm,datasetSpec.storageToken)
                        # use default endpoint
                        if token == None:
                            token = siteSpec.ddm
                        # add origianl token
                        if not datasetSpec.storageToken in ['',None]:
                            token += '/{0}'.format(datasetSpec.storageToken)
                        retMap[tmpTaskID]['datasets'].append({'datasetID':datasetSpec.datasetID,
                                                              'token':'dst:{0}'.format(token),
                                                              'destination':tmpCoreName})
                else:
                    retMap[tmpTaskID] = tmpCoreName
        tmpLog.debug('ret {0}'.format(str(retMap)))
        # return
        tmpLog.debug('done')        
        return self.SC_SUCCEEDED,retMap



    # main to assign
    def doBrokerage(self,inputList,vo,prodSourceLabel,workQueue):
        # list with a lock
        inputListWorld = ListWithLock([])
        # variables for submission
        maxBunchTask = 100
        # make logger
        tmpLog = MsgWrapper(logger)
        tmpLog.debug('start doBrokerage')
        # return for failure
        retFatal    = self.SC_FATAL
        retTmpError = self.SC_FAILED
        tmpLog.debug('vo={0} label={1} queue={2} nTasks={3}'.format(vo,prodSourceLabel,
                                                                    workQueue.queue_name,
                                                                    len(inputList)))
        # loop over all tasks
        allRwMap    = {}
        prioMap     = {}
        tt2Map      = {}
        expRWs      = {}
        jobSpecList = []
        for tmpJediTaskID,tmpInputList in inputList:
            for taskSpec,cloudName,inputChunk in tmpInputList:
                # collect tasks for WORLD
                if taskSpec.useWorldCloud():
                    inputListWorld.append((taskSpec,inputChunk))
                    continue
                # make JobSpec to be submitted for TaskAssigner
                jobSpec = JobSpec()
                jobSpec.taskID     = taskSpec.jediTaskID
                jobSpec.jediTaskID = taskSpec.jediTaskID
                # set managed to trigger TA
                jobSpec.prodSourceLabel  = 'managed'
                jobSpec.processingType   = taskSpec.processingType
                jobSpec.workingGroup     = taskSpec.workingGroup
                jobSpec.metadata         = taskSpec.processingType
                jobSpec.assignedPriority = taskSpec.taskPriority
                jobSpec.currentPriority  = taskSpec.currentPriority
                jobSpec.maxDiskCount     = (taskSpec.getOutDiskSize() + taskSpec.getWorkDiskSize()) / 1024 / 1024
                if taskSpec.useWorldCloud():
                    # use destinationSE to trigger task brokerage in WORLD cloud
                    jobSpec.destinationSE = taskSpec.cloud
                prodDBlock = None
                setProdDBlock = False
                for datasetSpec in inputChunk.getDatasets():
                    prodDBlock = datasetSpec.datasetName
                    if datasetSpec.isMaster():
                        jobSpec.prodDBlock = datasetSpec.datasetName
                        setProdDBlock = True
                    for fileSpec in datasetSpec.Files:
                        tmpInFileSpec = fileSpec.convertToJobFileSpec(datasetSpec)
                        jobSpec.addFile(tmpInFileSpec)
                # use secondary dataset name as prodDBlock
                if setProdDBlock == False and prodDBlock != None:
                    jobSpec.prodDBlock = prodDBlock
                # append
                jobSpecList.append(jobSpec)
                prioMap[jobSpec.taskID] = jobSpec.currentPriority
                tt2Map[jobSpec.taskID]  = jobSpec.processingType
                # get RW for a priority
                if not allRwMap.has_key(jobSpec.currentPriority):
                    tmpRW = self.taskBufferIF.calculateRWwithPrio_JEDI(vo,prodSourceLabel,workQueue,
                                                                       jobSpec.currentPriority) 
                    if tmpRW == None:
                        tmpLog.error('failed to calculate RW with prio={0}'.format(jobSpec.currentPriority))
                        return retTmpError
                    allRwMap[jobSpec.currentPriority] = tmpRW
                # get expected RW
                expRW = self.taskBufferIF.calculateTaskRW_JEDI(jobSpec.jediTaskID)
                if expRW == None:
                    tmpLog.error('failed to calculate RW for jediTaskID={0}'.format(jobSpec.jediTaskID))
                    return retTmpError
                expRWs[jobSpec.taskID] = expRW
        # for old clouds
        if jobSpecList != []:
            # get fullRWs
            fullRWs = self.taskBufferIF.calculateRWwithPrio_JEDI(vo,prodSourceLabel,None,None)
            if fullRWs == None:
                tmpLog.error('failed to calculate full RW')
                return retTmpError
            # set metadata
            for jobSpec in jobSpecList:
                rwValues = allRwMap[jobSpec.currentPriority]
                jobSpec.metadata = "%s;%s;%s;%s;%s;%s" % (jobSpec.metadata,
                                                          str(rwValues),str(expRWs),
                                                          str(prioMap),str(fullRWs),
                                                          str(tt2Map))
            tmpLog.debug('run task assigner for {0} tasks'.format(len(jobSpecList)))
            nBunchTask = 0
            while nBunchTask < len(jobSpecList):
                # get a bunch
                jobsBunch = jobSpecList[nBunchTask:nBunchTask+maxBunchTask]
                strIDs = 'jediTaskID='
                for tmpJobSpec in jobsBunch:
                    strIDs += '{0},'.format(tmpJobSpec.taskID)
                strIDs = strIDs[:-1]
                tmpLog.debug(strIDs)
                # increment index
                nBunchTask += maxBunchTask
                # run task brokerge
                stS,outSs = PandaClient.runTaskAssignment(jobsBunch)
                tmpLog.debug('{0}:{1}'.format(stS,str(outSs)))
        # for WORLD
        if len(inputListWorld) > 0:
            # thread pool
            threadPool = ThreadPool()
            # get full RW for WORLD
            fullRWs = self.taskBufferIF.calculateWorldRWwithPrio_JEDI(vo,prodSourceLabel,None,None)
            if fullRWs == None:
                tmpLog.error('failed to calculate full WORLD RW')
                return retTmpError
            # get RW per priority
            for taskSpec,inputChunk in inputListWorld:
                if not taskSpec.currentPriority in allRwMap:
                    tmpRW = self.taskBufferIF.calculateWorldRWwithPrio_JEDI(vo,prodSourceLabel,workQueue,
                                                                            taskSpec.currentPriority)
                    if tmpRW == None:
                        tmpLog.error('failed to calculate RW with prio={0}'.format(taskSpec.currentPriority))
                        return retTmpError
                    allRwMap[taskSpec.currentPriority] = tmpRW
            # live counter for RWs
            liveCounter = MapWithLock(allRwMap)
            # make workers
            ddmIF = self.ddmIF.getInterface(vo)
            for iWorker in range(4):
                thr = AtlasProdTaskBrokerThread(inputListWorld,threadPool,
                                                self.taskBufferIF,ddmIF,
                                                fullRWs,liveCounter)
                thr.start()
            threadPool.join(60*10)
        # return
        tmpLog.debug('doBrokerage done')
        return self.SC_SUCCEEDED



    # check file availability
    def findMissingFiles(self,jediTaskID,cloudName):
        tmpLog = MsgWrapper(logger,'<jediTaskID={0}>'.format(jediTaskID))
        tmpLog.debug('start findMissingFiles')
        # return for failure
        retError = self.SC_FAILED
        # get datasets
        tmpSt,datasetSpecList = self.taskBufferIF.getDatasetsWithJediTaskID_JEDI(jediTaskID,['input'],True)
        if not tmpSt:
            tmpLog.error('failed to get the list of datasets')
            return retError
        # loop over all datasets
        for datasetSpec in datasetSpecList: 
            # check only master dataset
            if not datasetSpec.isMaster():
                continue
            tmpLog.debug('checking {0}'.format(datasetSpec.datasetName))
            # get ddmIF
            ddmIF = self.ddmIF.getInterface(datasetSpec.vo)
            if ddmIF == None:
                tmpLog.error('failed to get DDM I/F for vo={0}'.format(datasetSpec.vo))
                return retError
            # get the list of sites where data is available
            tmpSt,tmpRet = AtlasBrokerUtils.getSitesWithData(self.siteMapper,ddmIF,
                                                             datasetSpec.datasetName)
            if tmpSt != self.SC_SUCCEEDED:
                tmpLog.error('failed to get the list of sites where {0} is available, since {1}'.format(datasetSpec.datasetName,
                                                                                                        tmpRet))
                return retError
            dataSiteMap = tmpRet
            # data is unavailable in cloud
            if not dataSiteMap.has_key(cloudName):
                tmpLog.error('{0} is unavailable in cloud={1} map={2}'.format(datasetSpec.datasetName,cloudName,str(dataSiteMap)))
                return retError
            # mapping between sites and storage endpoints
            checkedSites = [self.siteMapper.getCloud(cloudName)['source']]+dataSiteMap[cloudName]['t2']
            siteStorageEP = AtlasBrokerUtils.getSiteStorageEndpointMap(checkedSites,self.siteMapper)
            # get available files per site/endpoint                                                                                     
            tmpAvFileMap = ddmIF.getAvailableFiles(datasetSpec,
                                                   siteStorageEP,
                                                   self.siteMapper,
                                                   ngGroup=[1],
                                                   checkLFC=True)
            if tmpAvFileMap == None:
                tmpLog.error('failed to get available file list for {0}'.format(datasetSpec.datasetName))
                return retError
            # check availability
            missingFiles = []
            for fileSpec in datasetSpec.Files:
                fileFound = False
                for tmpSiteName,availableFilesMap in tmpAvFileMap.iteritems():
                    for tmpStorageType,availableFiles in availableFilesMap.iteritems():
                        for availableFile in availableFiles:
                            if fileSpec.lfn == availableFile.lfn:
                                fileFound = True
                                break
                        if fileFound:
                            break
                    if fileFound:
                        break
                # missing
                if not fileFound:
                    missingFiles.append(fileSpec.fileID)
                    tmpLog.debug('{0} missing'.format(fileSpec.lfn))
            # update contents
            if missingFiles != []:        
                tmpSt = self.taskBufferIF.setMissingFiles_JEDI(jediTaskID,datasetSpec.datasetID,missingFiles)
                if not tmpSt:
                    tmpLog.error('failed to set missing files in {0}'.format(datasetSpec.datasetName))
                    return retError
        tmpLog.debug('done findMissingFiles')
        return self.SC_SUCCEEDED



# thread for real worker
class AtlasProdTaskBrokerThread (WorkerThread):

    # constructor
    def __init__(self,inputList,threadPool,taskbufferIF,ddmIF,
                 fullRW,prioRW):
        # initialize woker with no semaphore
        WorkerThread.__init__(self,None,threadPool,logger)
        # attributres
        self.inputList    = inputList
        self.taskBufferIF = taskbufferIF
        self.ddmIF        = ddmIF
        self.msgType      = 'taskbrokerage'
        self.fullRW       = fullRW
        self.prioRW       = prioRW
        self.numTasks     = 0


    # wrapper for return
    def sendLogMessage(self,tmpLog):
        # send info to logger
        tmpLog.bulkSendMsg('taskbrokerage',loggerName='bamboo')
        tmpLog.debug('sent')


    # main function
    def runImpl(self):
        # cutoff for disk in TB
        diskThreshold = 100 * 1024
        # dataset type to ignore file availability check
        datasetTypeToSkipCheck = ['log']
        thrInputSize = 1024*1024*1024
        thrInputNum = 100
        thrInputSizeFrac = 0.1
        thrInputNumFrac = 0.1
        cutOffRW = 50
        negWeightTape = 0.001
        # main
        lastJediTaskID = None
        siteMapper = self.taskBufferIF.getSiteMapper()
        while True:
            try:
                taskInputList = self.inputList.get(1)
                # no more datasets
                if len(taskInputList) == 0:
                    self.logger.debug('{0} terminating after processing {1} tasks since no more inputs '.format(self.__class__.__name__,
                                                                                                                self.numTasks))
                    return
                # loop over all tasks
                for taskSpec,inputChunk in taskInputList:
                    lastJediTaskID = taskSpec.jediTaskID
                    # make logger
                    tmpLog = MsgWrapper(self.logger,'<jediTaskID={0}>'.format(taskSpec.jediTaskID),monToken='jediTaskID={0}'.format(taskSpec.jediTaskID))
                    tmpLog.debug('start')
                    # RW
                    taskRW = self.taskBufferIF.calculateTaskWorldRW_JEDI(taskSpec.jediTaskID)
                    # get nuclei
                    nucleusList = siteMapper.nuclei
                    if taskSpec.nucleus in nucleusList:
                        candidateNucleus = taskSpec.nucleus
                    else:
                        tmpLog.debug('got {0} candidates'.format(len(nucleusList)))
                        ######################################
                        # check status
                        newNucleusList = {}
                        for tmpNucleus,tmpNucleusSpec in nucleusList.iteritems():
                            if not tmpNucleusSpec.state in ['ACTIVE']:
                                tmpLog.debug('  skip nucleus={0} due to status={1} criteria=-status'.format(tmpNucleus,
                                                                                                            tmpNucleusSpec.state))
                            else:
                                newNucleusList[tmpNucleus] = tmpNucleusSpec
                        nucleusList = newNucleusList
                        tmpLog.debug('{0} candidates passed status check'.format(len(nucleusList)))
                        if nucleusList == {}:
                            tmpLog.error('no candidates')
                            taskSpec.setErrDiag(tmpLog.uploadLog(taskSpec.jediTaskID))
                            self.sendLogMessage(tmpLog)
                            continue
                        ######################################
                        # check status of transfer backlog
                        newNucleusList = {}
                        backlogged_nuclei = self.taskBufferIF.getBackloggedNuclei()
                        for tmpNucleus, tmpNucleusSpec in nucleusList.iteritems():
                            if tmpNucleus in backlogged_nuclei:
                                tmpLog.debug('  skip nucleus={0} due to long transfer backlog criteria=-transfer_backlog'.
                                             format(tmpNucleus))
                            else:
                                newNucleusList[tmpNucleus] = tmpNucleusSpec
                        nucleusList = newNucleusList
                        tmpLog.debug('{0} candidates passed status check'.format(len(nucleusList)))
                        if nucleusList == {}:
                            tmpLog.error('no candidates')
                            taskSpec.setErrDiag(tmpLog.uploadLog(taskSpec.jediTaskID))
                            self.sendLogMessage(tmpLog)
                            continue
                        ######################################
                        # check endpoint
                        fractionFreeSpace = {}
                        newNucleusList = {}
                        tmpStat,tmpDatasetSpecList = self.taskBufferIF.getDatasetsWithJediTaskID_JEDI(taskSpec.jediTaskID,
                                                                                                      ['output','log'])
                        for tmpNucleus,tmpNucleusSpec in nucleusList.iteritems():
                            toSkip = False
                            for tmpDatasetSpec in tmpDatasetSpecList:
                                # ignore distributed datasets
                                if DataServiceUtils.getDistributedDestination(tmpDatasetSpec.storageToken) != None:
                                    continue
                                # get endpoint with the pattern
                                tmpEP = tmpNucleusSpec.getAssoicatedEndpoint(tmpDatasetSpec.storageToken)
                                if tmpEP == None:
                                    tmpLog.debug('  skip nucleus={0} since no endpoint with {1} criteria=-match'.format(tmpNucleus,
                                                                                                                        tmpDatasetSpec.storageToken))
                                    toSkip = True
                                    break
                                # check state
                                """
                                if not tmpEP['state'] in ['ACTIVE']:
                                    tmpLog.debug('  skip nucleus={0} since endpoint {1} is in {2} criteria=-epstatus'.format(tmpNucleus,
                                                                                                                             tmpEP['ddm_endpoint_name'],
                                                                                                                             tmpEP['state']))
                                    toSkip = True
                                    break
                                """    
                                # check space
                                tmpSpaceSize = tmpEP['space_free'] + tmpEP['space_expired']
                                tmpSpaceToUse = 0
                                if tmpNucleus in self.fullRW:
                                    # 0.25GB per cpuTime/corePower/day
                                    tmpSpaceToUse = long(self.fullRW[tmpNucleus]/10/24/3600*0.25)
                                if tmpSpaceSize-tmpSpaceToUse < diskThreshold:
                                    tmpLog.debug('  skip nucleus={0} since disk shortage (free {1} + reserved {2} < thr {3}) at endpoint {4} criteria=-space'.format(tmpNucleus,
                                                                                                                                                                     tmpSpaceSize,
                                                                                                                                                                     tmpSpaceToUse,
                                                                                                                                                                     diskThreshold,
                                                                                                                                                                     tmpEP['ddm_endpoint_name']))
                                    toSkip = True
                                    break
                                # keep fraction of free space
                                if not tmpNucleus in fractionFreeSpace:
                                    fractionFreeSpace[tmpNucleus] = {'total':0,'free':0}
                                try:
                                    tmpOld = float(fractionFreeSpace[tmpNucleus]['free']) / \
                                        float(fractionFreeSpace[tmpNucleus]['total'])
                                except:
                                    tmpOld = None
                                try:
                                    tmpNew = float(tmpSpaceSize-tmpSpaceToUse)/float(tmpEP['space_total'])
                                except:
                                    tmpNew = None
                                if tmpNew != None and (tmpOld == None or tmpNew < tmpOld):
                                    fractionFreeSpace[tmpNucleus] = {'total':tmpEP['space_total'],
                                                                     'free':tmpSpaceSize-tmpSpaceToUse}
                            if not toSkip:
                                newNucleusList[tmpNucleus] = tmpNucleusSpec
                        nucleusList = newNucleusList
                        tmpLog.debug('{0} candidates passed endpoint check'.format(len(nucleusList)))
                        if nucleusList == {}:
                            tmpLog.error('no candidates')
                            taskSpec.setErrDiag(tmpLog.uploadLog(taskSpec.jediTaskID))
                            self.sendLogMessage(tmpLog)
                            continue
                        ###################################### 
                        # data locality
                        toSkip = False
                        availableData = {}
                        for datasetSpec in inputChunk.getDatasets():
                            # only for real datasets
                            if datasetSpec.isPseudo():
                                continue
                            # ignore DBR
                            if DataServiceUtils.isDBR(datasetSpec.datasetName):
                                continue
                            # skip locality check
                            if DataServiceUtils.getDatasetType(datasetSpec.datasetName) in datasetTypeToSkipCheck:
                                continue
                            # use deep scan for primary dataset
                            if datasetSpec.isMaster():
                                deepScan = True
                            else:
                                deepScan = False
                            # get nuclei where data is available
                            tmpSt,tmpRet = AtlasBrokerUtils.getNucleiWithData(siteMapper,self.ddmIF,
                                                                              datasetSpec.datasetName,
                                                                              nucleusList.keys(),
                                                                              deepScan)
                            if tmpSt != Interaction.SC_SUCCEEDED:
                                tmpLog.error('failed to get nuclei where data is available, since {0}'.format(tmpRet))
                                taskSpec.setErrDiag(tmpLog.uploadLog(taskSpec.jediTaskID))
                                self.sendLogMessage(tmpLog)
                                toSkip = True
                                break
                            # sum
                            for tmpNucleus,tmpVals in tmpRet.iteritems():
                                if not tmpNucleus in availableData:
                                    availableData[tmpNucleus] = tmpVals
                                else:
                                    availableData[tmpNucleus] = dict((k,v+tmpVals[k]) for (k,v) in availableData[tmpNucleus].iteritems())
                        if toSkip:
                            continue
                        if availableData != {}:
                            newNucleusList = {}
                            # skip if no data
                            skipMsgList = []
                            for tmpNucleus,tmpNucleusSpec in nucleusList.iteritems():
                                if len(nucleusList) == 1:
                                    tmpLog.debug('  disable data locality check for nucleus={0} since no other candidate'.format(tmpNucleus))
                                    newNucleusList[tmpNucleus] = tmpNucleusSpec
                                elif availableData[tmpNucleus]['tot_size'] > thrInputSize and \
                                        availableData[tmpNucleus]['ava_size_any'] < availableData[tmpNucleus]['tot_size'] * thrInputSizeFrac:
                                    tmpMsg = '  skip nucleus={0} due to insufficient input size {1}B < {2}*{3} criteria=-insize'.format(tmpNucleus,
                                                                                                                                        availableData[tmpNucleus]['ava_size_any'],
                                                                                                                                        availableData[tmpNucleus]['tot_size'],
                                                                                                                                        thrInputSizeFrac)
                                    skipMsgList.append(tmpMsg)
                                elif availableData[tmpNucleus]['tot_num'] > thrInputNum and \
                                        availableData[tmpNucleus]['ava_num_any'] < availableData[tmpNucleus]['tot_num'] * thrInputNumFrac:
                                    tmpMsg = '  skip nucleus={0} due to short number of input files {1} < {2}*{3} criteria=-innum'.format(tmpNucleus,
                                                                                                                                          availableData[tmpNucleus]['ava_num_any'],
                                                                                                                                          availableData[tmpNucleus]['tot_num'],
                                                                                                                                          thrInputNumFrac)
                                    skipMsgList.append(tmpMsg)
                                else:
                                    newNucleusList[tmpNucleus] = tmpNucleusSpec
                            if len(newNucleusList) > 0:
                                nucleusList = newNucleusList
                                for tmpMsg in skipMsgList:
                                    tmpLog.debug(tmpMsg)
                            else:
                                tmpLog.debug('  disable data locality check since no nucleus has input data')
                            tmpLog.debug('{0} candidates passed data check'.format(len(nucleusList)))
                            if nucleusList == {}:
                                tmpLog.error('no candidates')
                                taskSpec.setErrDiag(tmpLog.uploadLog(taskSpec.jediTaskID))
                                self.sendLogMessage(tmpLog)
                                continue
                        ######################################
                        # ability to execute jobs
                        newNucleusList = {}
                        # get all panda sites
                        tmpSiteList = []
                        for tmpNucleus,tmpNucleusSpec in nucleusList.iteritems():
                            tmpSiteList += tmpNucleusSpec.allPandaSites
                        tmpSiteList = list(set(tmpSiteList))
                        tmpLog.debug('===== start for job check')
                        jobBroker = AtlasProdJobBroker(self.ddmIF,self.taskBufferIF)
                        tmpSt,tmpRet = jobBroker.doBrokerage(taskSpec,taskSpec.cloud,inputChunk,None,True,
                                                             tmpSiteList,tmpLog)
                        tmpLog.debug('===== done for job check')
                        if tmpSt != Interaction.SC_SUCCEEDED:
                            tmpLog.debug('failed to get sites where jobs can run. Use any nuclei where input is available')
                            # use any nuclei where input is available if no sites can run jobs
                            tmpRet = tmpSiteList
                        okNuclei = set()
                        for tmpSite in tmpRet:
                            siteSpec = siteMapper.getSite(tmpSite)
                            okNuclei.add(siteSpec.pandasite)
                        for tmpNucleus,tmpNucleusSpec in nucleusList.iteritems():
                            if tmpNucleus in okNuclei:
                                newNucleusList[tmpNucleus] = tmpNucleusSpec
                            else:
                                tmpLog.debug('  skip nucleus={0} due to missing ability to run jobs criteria=-job'.format(tmpNucleus))
                        nucleusList = newNucleusList
                        tmpLog.debug('{0} candidates passed job check'.format(len(nucleusList)))
                        if nucleusList == {}:
                            tmpLog.error('no candidates')
                            taskSpec.setErrDiag(tmpLog.uploadLog(taskSpec.jediTaskID))
                            self.sendLogMessage(tmpLog)
                            continue
                        ###################################### 
                        # weight
                        self.prioRW.acquire()
                        nucleusRW = self.prioRW[taskSpec.currentPriority]
                        self.prioRW.release()
                        totalWeight = 0
                        nucleusweights = []
                        for tmpNucleus,tmpNucleusSpec in nucleusList.iteritems():
                            if not tmpNucleus in nucleusRW:
                                nucleusRW[tmpNucleus] = 0
                            wStr = '1'
                            # with RW
                            if tmpNucleus in nucleusRW and nucleusRW[tmpNucleus] >= cutOffRW:
                                weight = 1 / float(nucleusRW[tmpNucleus])
                                wStr += '/( RW={0} )'.format(nucleusRW[tmpNucleus])
                            else:
                                weight = 1
                                wStr += '/(1 : RW={0}<{1})'.format(nucleusRW[tmpNucleus],cutOffRW)
                            # with data
                            if availableData != {}:
                                if availableData[tmpNucleus]['tot_size'] > 0:
                                    weight *= float(availableData[tmpNucleus]['ava_size_any'])
                                    weight /= float(availableData[tmpNucleus]['tot_size'])
                                    wStr += '* ( available_input_size_DISKTAPE={0} )'.format(availableData[tmpNucleus]['ava_size_any'])
                                    wStr += '/ ( total_input_size={0} )'.format(availableData[tmpNucleus]['tot_size'])
                                    # negative weight for tape
                                    if availableData[tmpNucleus]['ava_size_any'] > availableData[tmpNucleus]['ava_size_disk']:
                                        weight *= negWeightTape
                                        wStr += '*( weight_TAPE={0} )'.format(negWeightTape)
                                # fraction of free space
                                if tmpNucleus in fractionFreeSpace:
                                    try:
                                        tmpFrac = float(fractionFreeSpace[tmpNucleus]['free']) / \
                                            float(fractionFreeSpace[tmpNucleus]['total'])
                                        weight *= tmpFrac
                                        wStr += '*( free_space={0} )/( total_space={1} )'.format(fractionFreeSpace[tmpNucleus]['free'],
                                                                                             fractionFreeSpace[tmpNucleus]['total'])
                                    except:
                                        pass
                            tmpLog.debug('  use nucleus={0} weight={1} {2} criteria=+use'.format(tmpNucleus,weight,wStr))
                            totalWeight += weight
                            nucleusweights.append((tmpNucleus,weight))
                        tmpLog.debug('final {0} candidates'.format(len(nucleusList)))
                        ###################################### 
                        # final selection
                        tgtWeight = random.uniform(0,totalWeight)
                        candidateNucleus = None
                        for tmpNucleus,weight in nucleusweights:
                            tgtWeight -= weight
                            if tgtWeight <= 0:
                                candidateNucleus = tmpNucleus
                                break
                        if candidateNucleus == None:
                            candidateNucleus = nucleusweights[-1][0]
                    ###################################### 
                    # update
                    nucleusSpec = nucleusList[candidateNucleus]
                    # get output/log datasets
                    tmpStat,tmpDatasetSpecs = self.taskBufferIF.getDatasetsWithJediTaskID_JEDI(taskSpec.jediTaskID,
                                                                                               ['output','log'])
                    # get destinations
                    retMap = {taskSpec.jediTaskID: AtlasBrokerUtils.getDictToSetNucleus(nucleusSpec,tmpDatasetSpecs)}
                    tmpRet = self.taskBufferIF.setCloudToTasks_JEDI(retMap)
                    tmpLog.info('  set nucleus={0} with {1} criteria=+set'.format(candidateNucleus,tmpRet))
                    self.sendLogMessage(tmpLog)
                    if tmpRet:
                        tmpMsg = 'set task.status=ready'
                        tmpLog.sendMsg(tmpMsg,self.msgType)
                    # update RW table
                    self.prioRW.acquire()
                    for prio,rwMap in self.prioRW.iteritems():
                        if prio > taskSpec.currentPriority:
                            continue
                        if candidateNucleus in rwMap:
                            rwMap[candidateNucleus] += taskRW
                        else:
                            rwMap[candidateNucleus] = taskRW
                    self.prioRW.release()
            except:
                errtype,errvalue = sys.exc_info()[:2]
                errMsg  = '{0}.runImpl() failed with {1} {2} '.format(self.__class__.__name__,errtype.__name__,errvalue)
                errMsg += 'lastJediTaskID={0} '.format(lastJediTaskID)
                errMsg += traceback.format_exc()
                logger.error(errMsg)
            
