import os, sys, time, re, json, pprint, datetime
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vmms.ec2SSH import Ec2SSH
from preallocator import Preallocator
from tangoObjects import TangoQueue
from tangoObjects import TangoMachine
from tango import TangoServer
from config import Config
import tangoObjects
import config_for_run_jobs
import redis
import boto3
import pytz
import argparse

# Read aws instances, Tango preallocator pools, etc.
# Also serve as sample code for quick testing of Tango/VMMS functionalities.

class CommandLine():
  def __init__(self):
    parser = argparse.ArgumentParser(
      description='List AWS vms and preallocator pools')
    parser.add_argument('-a', '--accessIdKeyUser',
                        help="aws access id, key and user, space separated")
    parser.add_argument('-c', '--createVMs', action='store_true',
                        dest='createVMs', help="add a VM for each pool")
    parser.add_argument('-d', '--destroyVMs', action='store_true',
                        dest='destroyVMs', help="destroy VMs and empty pools")
    parser.add_argument('-D', '--instanceNameTags', nargs='+',
                        help="destroy instances by name tags or AWS ids (can be partial).  \"None\" (case insensitive) deletes all instances without a \"Name\" tag")
    parser.add_argument('-l', '--list', action='store_true',
                        dest='listVMs', help="list and ping live vms")
    parser.add_argument('-L', '--listAll', action='store_true',
                        dest='listInstances', help="list all instances")
    self.args = parser.parse_args()

cmdLine = CommandLine()
argDestroyInstanceByNameTags = cmdLine.args.instanceNameTags
argListVMs = cmdLine.args.listVMs
argListAllInstances = cmdLine.args.listInstances
argDestroyVMs = cmdLine.args.destroyVMs
argCreateVMs = cmdLine.args.createVMs
argAccessIdKeyUser = cmdLine.args.accessIdKeyUser

def destroyVMs():
  vms = ec2.getVMs()
  print "number of Tango VMs:", len(vms)
  for vm in vms:
    if vm.id:    
      print "destroy", nameToPrint(vm.name)
      ec2.destroyVM(vm)
    else:
      print "VM not in Tango naming pattern:", nameToPrint(vm.name)
      
def pingVMs():
  vms = ec2.getVMs()
  print "number of Tango VMs:", len(vms)
  for vm in vms:
    if vm.id:
      print "ping", nameToPrint(vm.name)
      # Note: following call needs the private key file for aws to be
      # at wherever SECURITY_KEY_PATH in config.py points to.
      # For example, if SECURITY_KEY_PATH = '/root/746-autograde.pem',
      # then the file should exist there.
      ec2.waitVM(vm, Config.WAITVM_TIMEOUT)
    else:
      print "VM not in Tango naming pattern:", nameToPrint(vm.name)

local_tz = pytz.timezone("EST")
def utc_to_local(utc_dt):
  local_dt = utc_dt.replace(tzinfo=pytz.utc).astimezone(local_tz)
  return local_dt.strftime("%Y%m%d-%H:%M:%S")

# to test destroying instances without "Name" tag
def deleteNameTagForAllInstances():
  instances = listInstances()
  for instance in instances:
    boto3connection.delete_tags(Resources=[instance["Instance"].id],
                                Tags=[{"Key": "Name"}])
  print "Afterwards"
  print "----------"
  listInstances()

# to test changing tags to keep the vm after test failure
def changeTagForAllInstances():
  instances = listInstances()
  for inst in instances:
    instance = inst["Instance"]
    name = inst["Name"]
    notes = "tag " + name + " deleted"
    boto3connection.delete_tags(Resources=[instance["InstanceId"]],
                                Tags=[{"Key": "Name"}])
    boto3connection.create_tags(Resources=[instance["InstanceId"]],
                                Tags=[{"Key": "Name", "Value": "failed-" + name},
                                      {"Key": "Notes", "Value": notes}])

  print "Afterwards"
  print "----------"
  listInstances()

def listInstances(all=None):
  nameAndInstances = []

  filters=[]
  instanceType = "all"
  if not all:
    filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
    instanceType = "running"

  instances = boto3resource.instances.filter(Filters=filters)
  for instance in boto3resource.instances.filter(Filters=filters):
    nameAndInstances.append({"Name": ec2.getTag(instance.tags, "Name"),
                          "Instance": instance})

  nameAndInstances.sort(key=lambda x: x["Name"])
  print "number of", instanceType, "AWS instances:", len(nameAndInstances)

  for item in nameAndInstances:
    instance = item["Instance"]
    launchTime = utc_to_local(instance.launch_time)
    if instance.public_ip_address:
      print("%s: %s %s %s %s" %
            (nameToPrint(item["Name"]), instance.id,
             launchTime, instance.state["Name"],
             instance.public_ip_address))
    else:
      print("%s: %s %s %s" %
            (nameToPrint(item["Name"]), instance.id,
             launchTime, instance.state["Name"]))

    if instance.tags:
      for tag in instance.tags:
        if (tag["Key"] != "Name"):
          print("\t tag {%s: %s}" % (tag["Key"], tag["Value"]))
    else:
      print("\t No tags")

    print "\t InstanceType:", instance.instance_type
    """ useful sometimes
    image = boto3resource.Image(instance.image_id)
    print "\t ImageId:", image.image_id
    for tag in image.tags:
      print("\t\t image tag {%s: %s}" % (tag["Key"], tag["Value"]))
    """

  return nameAndInstances

def listPools():
  print "known AWS images:", ec2.img2ami.keys()
  knownPools = server.preallocator.machines.keys()
  print "Tango VM pools:", "" if knownPools else "None"

  for key in knownPools:
    pool = server.preallocator.getPool(key)
    totalPool = pool["total"]
    freePool = pool["free"]
    totalPool.sort()
    freePool.sort()
    print "pool", nameToPrint(key), "total", len(totalPool), totalPool, freePool

def nameToPrint(name):
    return "[" + name + "]" if name else "[None]"

# allocate "num" vms for each and every pool (image)
def addVMs():
  # Add a vm for each image and a vm for the first image plus instance type
  instanceTypeTried = False
  for key in ec2.img2ami.keys():
    vm = TangoMachine(vmms="ec2SSH", image=key)
    pool = server.preallocator.getPool(vm.pool)
    currentCount = len(pool["total"]) if pool else 0
    print "adding a vm into pool", nameToPrint(vm.pool), "current size", currentCount
    server.preallocVM(vm, currentCount + 1)

    if instanceTypeTried:
      continue
    else:
      instanceTypeTried = True

    vm = TangoMachine(vmms="ec2SSH", image=key+"+t2.small")
    pool = server.preallocator.getPool(vm.pool)
    currentCount = len(pool["total"]) if pool else 0
    print "adding a vm into pool", nameToPrint(vm.pool), "current size", currentCount
    server.preallocVM(vm, currentCount + 1)

def destroyRedisPools():
  for key in server.preallocator.machines.keys():
    print "clean up pool", key
    server.preallocator.machines.set(key, [[], TangoQueue(key)])
    server.preallocator.machines.get(key)[1].make_empty()

# END of function definitions #

# When a host has two Tango containers (for experiment), there are two
# redis servers, too.  They differ by the forwarding port number, which
# is defined in config_for_run_jobs.py.  To select the redis server,
# We get the connection here and pass it into tangoObjects
redisConnection = redis.StrictRedis(
  host=Config.REDIS_HOSTNAME, port=config_for_run_jobs.Config.redisHostPort, db=0)
tangoObjects.getRedisConnection(connection=redisConnection)
boto3connection = boto3.client("ec2", Config.EC2_REGION)
boto3resource = boto3.resource("ec2", Config.EC2_REGION)

server = TangoServer()
ec2 = server.preallocator.vmms["ec2SSH"]
pools = ec2.img2ami

if argDestroyInstanceByNameTags:
  nameAndInstances = listInstances()
  totalTerminated = []

  matchingInstances = []
  for partialStr in argDestroyInstanceByNameTags:
    if partialStr.startswith("i-"):  # match instance id
      for item in nameAndInstances:
        if item["Instance"].id.startswith(partialStr):
          matchingInstances.append(item)
    else:
      # part of "Name" tag or None to match instances without name tag
      for item in nameAndInstances:
        nameTag = ec2.getTag(item["Instance"].tags, "Name")
        if nameTag and \
           (nameTag.startswith(partialStr) or nameTag.endswith(partialStr)):
          matchingInstances.append(item)
        elif not nameTag and partialStr == "None":
          matchingInstances.append(item)

  # the loop above may generate duplicates in matchingInstances
  terminatedInstances = []
  for item in matchingInstances:
    if item["Instance"].id not in terminatedInstances:
      boto3connection.terminate_instances(InstanceIds=[item["Instance"].id])
      terminatedInstances.append(item["Instance"].id)

  if terminatedInstances:
    print "terminate %d instances matching query string \"%s\":" % \
      (len(terminatedInstances), argDestroyInstanceByNameTags)
    for id in terminatedInstances:
      print id
    print "Afterwards"
    print "----------"
    listInstances()
  else:
    print "no instances matching query string \"%s\"" % argDestroyInstanceByNameTags

  exit()

if argListAllInstances:
  listInstances("all")
  exit()

if argListVMs:
  listInstances()
  listPools()
  pingVMs()
  exit()

if argDestroyVMs:
  destroyVMs()
  destroyRedisPools()
  print "Afterwards"
  print "----------"
  listInstances()
  listPools()
  exit()

if argCreateVMs:
  listInstances()
  listPools()
  addVMs()  # add 1 vm for each image and each image plus instance type
  listInstances()
  listPools()
  exit()

# ec2WithKey can be used to test the case that tango_cli uses
# non-default aws access id and key

# to test, run
# sudo python tools/ec2Read.py -a "accessKeyId accessKey user image"
# accessKeyId and accessKey can be found in boto.cfg, user should exist on the image
# and the image must allow aws to install the public
# part of the ssh key to the vm at startup time.  Our autograde images are not
# suitable because they have the public key preinstalled and disallow aws to install.
# So far, assume the image is a stock aws image with a Name tag containing "test".

# to manually test the ssh command, if things doesn't work as expected,
# create a vm using an image with autograder, do
# sudo ssh -v -i deployment/config/746-autograde.pem -o "StrictHostKeyChecking no" -o "GSSAPIAuthentication no" autolab@<ip of the vm>

# to manually test the ssh command using generated pem file with access id/key is
# in use, create a vm using the special "test" image (see above), do
# sudo ssh -v -i /<vm name>.pem -o "StrictHostKeyChecking no" -o "GSSAPIAuthentication no" <user>@<ip of the vm>
# like
# sudo ssh -v -i /pdl-10002-sshtest.pem  -o "StrictHostKeyChecking no" -o "GSSAPIAuthentication no" ec2-user@18.212.159.60

if argAccessIdKeyUser:
  if len(argAccessIdKeyUser.split()) != 4:
    print "access id, key, user and image must be quoted and space separated"
    exit()
  (id, key, user, image) = argAccessIdKeyUser.split()
  ec2WithKey = Ec2SSH(accessKeyId=id, accessKey=key, ec2User=user)
  vm = TangoMachine(image=image)
  vm.id = int(30000)  # a high enough number to avoid collision with existing vms
  specialVM = ec2WithKey.initializeVM(vm)
  if not specialVM:
      print "exit after failure to get special vm"
      exit()
  if ec2WithKey.waitVM(specialVM, Config.WAITVM_TIMEOUT) != 0:
      print "exit after failure to wait for vm", specialVM.name
      exit()
  print "Instances after creating special vm", specialVM.name
  listInstances()

  # delete vm, the key pair for vm acces from aws, the /vmName.pem file
  vmName = specialVM.name
  ec2WithKey.destroyVM(specialVM)

  # Also create vm without the special access key
  normalImage = None
  for img in ec2.img2ami.keys():
      if not re.search('test', img, re.IGNORECASE):
          normalImage = img
          print "found non-test image", normalImage
          break

  vm = TangoMachine(image=normalImage)
  vm.id = int(30001)  # a high enough number to avoid collisio with existing vms
  normalVM = ec2.initializeVM(vm)
  if not normalVM:
      print "exit after failure to get normal vm"
      exit()
  if ec2.waitVM(normalVM, Config.WAITVM_TIMEOUT) != 0:
      print "exit after failure to wait for vm", normalVM.name
      exit()
  print "Instances after creating normal vm", normalVM.name
  listInstances()
  ec2.destroyVM(normalVM)

  print "Instances after destroying both vm"
  listInstances()

# Write combination of ops not provided by the command line options here:
