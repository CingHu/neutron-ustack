#!/bin/bash

if [ $# -ne 2 ];then
   echo "./servicevm-deploy.sh ip_address vlan_id"
   echo "example: ./servicevm-deploy.sh 10.0.88.24 5"
   exit
fi

ip=$1
vlan=$2

ip link add mgmt_service0 type veth peer name mgmt_service1
ifconfig mgmt_service0 $ip/24 up
ifconfig mgmt_service0 up
ifconfig mgmt_service1 up
ovs-vsctl add-port br-int mgmt_service1 tag=$vlan
