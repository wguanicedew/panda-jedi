from pandajedi.jedimsgprocessor.base_msg_processor import BaseMsgProcPlugin

from pandacommon.pandalogger import logger_utils


# logger
base_logger = logger_utils.setup_logger(__name__.split('.')[-1])


# forwarding message processing plugin
class ForwardingMsgProcPlugin(BaseMsgProcPlugin):
    """
    Simply forward the message from one queue to another
    """
    def process(self, msg_obj):
        # logger
        tmp_log = logger_utils.make_logger(base_logger, method_name='process')
        # start
        # tmp_log.info('start')
        # tmp_log.debug('sub_id={0} ; msg_id={1}'.format(msg_obj.sub_id, msg_obj.msg_id))
        # run
        try:
            msg = msg_obj.data
            tmp_log.debug('forward message {0}'.format(msg))
        except Exception as e:
            err_str = 'failed to run, skipped. {0} : {1}'.format(e.__class__.__name__, e)
            tmp_log.error(err_str)
            raise
        # done
        # tmp_log.info('done')
        return msg