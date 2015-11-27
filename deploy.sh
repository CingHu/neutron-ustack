#!/bin/bash

VFW_IMAGE_NAME="HillstoneOS"
VFW_IMAGE="SG6000-VM02-5.5R1F1.qcow2"

#create servicevm user
SERVICE_TENANT=$(keystone tenant-list | awk -F"|" '/services/{print $2}')
keystone user-create --name servicevm --tenant $SERVICE_TENANT --pass servicevm --enabled true --email support@unitedstack.com
SERVICEVM_USER=$(keystone user-list | awk -F"|" '/servicevm/{print $2}')
ADMIN_ROLE=$(keystone user-role-list | awk -F"|" '/ admin /{print $2}')
keystone user-role-add --user $SERVICEVM_USER --role $ADMIN_ROLE --tenant $SERVICE_TENANT


#upload vfw image
glance image-create --name=$VFW_IMAGE_NAME --property hw_vif_model=virtio --disk-format=qcow2 --container-format=bare --is-public=true < $VFW_IMAGE


#install neutron
python setup.py install
