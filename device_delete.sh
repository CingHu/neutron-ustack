#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo $TOKEN
devices=$(neutron device-list | cut -d"|" -f2 | grep -v "id" | grep -v "+")
for d in $devices;do
neutron device-delete $d
done

#ports=$(neutron port-list --network-id=687856aa-3401-4771-aca3-fa5d4f1d6f56 | cut -d"|" -f2|grep -v "id"|grep -v "+")
#ports=$(neutron port-list | grep "10.0.88" | cut -d"|" -f2|grep -v "id"|grep -v "+")
#for p in $ports;do
#echo $p
#neutron port-delete $p
#done
