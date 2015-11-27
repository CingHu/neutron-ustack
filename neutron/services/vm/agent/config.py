# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013, 2014 Intel Corporation.
# Copyright 2013, 2014 Isaku Yamahata <isaku.yamahata at intel com>
#                                     <isaku.yamahata at gmail com>
# All Rights Reserved.
#
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Isaku Yamahata, Intel Corporation.

from oslo.config import cfg

OPTS = [
    cfg.IntOpt('rpc_loop_interval', default=60,
               help=_("Interval when the process_services() loop "
                      "executes in seconds. This is when the servicevm agent "
                      "lets each service helper to process its neutron "
                      "resources.")),
    cfg.IntOpt('device_connection_timeout', default=30,
               help=_("Time in seconds for connecting to a hosting device")),
    cfg.IntOpt('device_dead_timeout', default=300,
               help=_("The time in seconds until a backlogged hosting device "
                      "is presumed dead. This value should be set up high "
                      "enough to recover from a period of connectivity loss "
                      "or high load when the device may not be responding.")),
    cfg.BoolOpt('reset_ovs', default=False,
                help=_("Reset ovs flows when servicevm agent start if this set true, "
                       "it will cause servicevm's network break.")),
    cfg.IntOpt('http_pool_size', default=4,                                                                                                                    
               help=_("Number of threads to use to make HTTP requests")),          
    cfg.IntOpt('http_timeout', default=15,                                      
               help=_("device http timeout duration in seconds")),  
    cfg.StrOpt('svc_helper_class',
               default="neutron.services.vm.agent.service_helpers"
               ".service_helper.ServiceHelper",
               help=_("Path of the service helper class.")),
    cfg.StrOpt('external_device',
               default="eth1",
               help=_("External network device in service node.")),
    cfg.StrOpt('servicevm_dir',
               default='$state_path/servicevm',
               help=("Directory to store service instance status"))
]

