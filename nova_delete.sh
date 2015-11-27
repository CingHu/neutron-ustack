#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo $TOKEN
devices=$(nova list --all-tenants | grep DeviceNova | cut -d"|" -f2 | grep -v "id" | grep -v "+")
#for d in $devices;do
#echo $d
nova delete $devices
#done
