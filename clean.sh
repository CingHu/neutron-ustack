#!/bin/bash

instances=$(neutron service-instance-list | awk '{print $2}')
for a in $instances;do
    s=$(neutron service-instance-delete $a);
    echo $s;
done
