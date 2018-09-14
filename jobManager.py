#
# JobManager - Thread that assigns jobs to worker threads
#
# The job manager thread wakes up every so often, scans the job list
# for new unassigned jobs, and tries to assign them.
#
# Assigning a job will try to get a preallocated VM that is ready,
# otherwise will pass 'None' as the preallocated vm.  A worker thread
# is launched that will handle things from here on. If anything goes
# wrong, the job is made dead with the error.
#
import threading, logging, time, copy, os

from datetime import datetime
from tango import *
from jobQueue import JobQueue
from preallocator import Preallocator
from worker import Worker

from tangoObjects import TangoQueue
from config import Config

class JobManager:

    def __init__(self, queue):
        self.daemon = True
        self.jobQueue = queue
        self.preallocator = self.jobQueue.preallocator
        self.vmms = self.preallocator.vmms
        self.log = logging.getLogger("JobManager-" + str(os.getpid()))
        # job-associated instance id
        self.nextId = 10000
        self.running = False
        self.log.info("START jobManager")

    def start(self):
        if self.running:
            return
        thread = threading.Thread(target=self.__manage)
        thread.daemon = True
        thread.start()

    def run(self):
        if self.running:
            return
        self.__manage()

    def _getNextID(self):
        """ _getNextID - returns next ID to be used for a job-associated
        VM.  Job-associated VM's have 5-digit ID numbers between 10000
        and 99999.
        """
        id = self.nextId
        self.nextId += 1
        # xxxXXX??? simply wrap the id without guarding condition is bad. disable for now.
        # if self.nextId > 99999:
        #    self.nextId = 10000
        return id

    def __manage(self):
        self.running = True
        while True:
            time.sleep(Config.DISPATCH_PERIOD)

            id = self.jobQueue.getNextPendingJob()
            job = None if not id else self.jobQueue.get(id)
            if not job:
                self.log.info("_manager no job from job queue")
                continue

            if not job.accessKey and Config.REUSE_VMS:
                # when free pool is empty, allocate vms async
                if self.preallocator.freePoolSize(job.vm.pool) == 0 and \
                   self.preallocator.poolSize(job.vm.pool) < Config.POOL_SIZE:
                    increment = 1
                    if hasattr(Config, 'POOL_ALLOC_INCREMENT') and Config.POOL_ALLOC_INCREMENT:
                        increment = Config.POOL_ALLOC_INCREMENT
                    self.preallocator.incrementPoolSize(job.vm, increment)

                # now try to get a vm.  It may not work because 1. allocating vm async
                # is not done yet. Or 2. pool limit has reached.
                vm = self.preallocator.allocVM(job.vm.pool)
                if not vm:
                    self.log.info("_manager no vm allocated to job %s" % id)
                    continue
                self.log.info("_manager vm %s allocated to job %s" % (vm.name, id))

            try:
                if job.accessKeyId:
                    # if the job has specified special aws access id/key,
                    # create a special vmms using the id/key and carry the vmms
                    # in the job object.  Create a vm using the vmms and destroy
                    # it after the job.  preallocator is not used.

                    from vmms.ec2SSH import Ec2SSH
                    vmms = Ec2SSH(job.accessKeyId, job.accessKey)
                    newVM = copy.deepcopy(job.vm)
                    newVM.id = self._getNextID()
                    preVM = vmms.initializeVM(newVM)
                    self.log.info("_manage with aws access id/key: init vm %s for job %s" %
                                  (preVM.name, job.id))
                    self.jobQueue.assignJob(job.id)
                else:
                    # Try to find a vm on the free list and allocate it to
                    # the worker if successful.
                    if Config.REUSE_VMS:
                        self.jobQueue.assignJob(job.id)
                        self.log.info("_manage job %s assigned to %s for REUSE_VMS" %
                                      (id, vm.name))
                        preVM = vm
                        self.log.info("_manage use vm %s" % preVM.name)
                    else:
                        # xxxXXX??? strongly suspect this code path doesn't work.
                        # After setting REUSE_VMS to False, job submissions don't run.
                        preVM = self.preallocator.allocVM(job.vm.pool)
                        self.log.info("_manage allocate vm %s" % preVM.name)
                    vmms = self.vmms[job.vm.vmms]  # Create new vmms object

                # Now dispatch the job to a worker
                self.log.info("Dispatched job %s:%d to %s [try %d]" %
                              (job.name, job.id, preVM.name, job.retries))
                job.appendTrace("Dispatched job %s:%d to %s [try %d]" %
                                (job.name, job.id, preVM.name, job.retries))

                Worker(job,
                       vmms,
                       self.jobQueue,
                       self.preallocator,
                       preVM
                ).start()

            except Exception as err:
                if job is not None:
                    self.jobQueue.makeDead(job.id, str(err))
                else:
                    # when job has no vm, it may get here.  Not good coding practice
                    self.log.info("_manage: job is None %s", err)

        # END of while loop
    # END of __manage function

if __name__ == "__main__":

    if not Config.USE_REDIS:
        print("You need to have Redis running to be able to initiate stand-alone\
         JobManager")
    else:
        tango = TangoServer()
        tango.log.debug("Resetting Tango VMs")
        tango.resetTango(tango.preallocator.vmms)
        jobs = JobManager(tango.jobQueue)
        tango.log.info("Starting the stand-alone Tango JobManager")
        jobs.run()
