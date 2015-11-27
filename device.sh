#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo $TOKEN
#TOKEN="3ac866661cac476a903af4b6f90c9773"
curl -i 'http://127.0.0.1:9696/v2.0/vm/devices.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "device": {
        "template_id": "855d2fc6-fed8-44fc-ac74-a90c90fd9329",
        "auth":[{"username":"test1111", "password":"123456"}],
        "attributes": {
            "fixed_ips": [
                {
                    "subnet_id": "b82b6675-4040-4ebd-bc06-f762753b2811",
                    "gateway_ip":"True"
                }
            ]
        }
    }
}'

        #"template_id": "8316d5e9-8de3-4d09-b6a9-29b299cf7c07",
                    #"subnet_id": "77fbe5b9-13cc-44f5-b37c-998683767319"
#name='servicevm_mgmt_security_group'
#service_tenant=$(keystone tenant-list | grep -w services  | cut -d'|' -f2)
#echo $service_tenant
#security_group=$(neutron security-group-create --tenant-id $service_tenant  $name |grep ' id ' | awk '{print $2}' 
#echo $security_group
#neutron security-group-rule-create $security_group --tenant-id $service_tenant --direction egress --ethertype IPv4 --protocol tcp --port-range-min 80 --port-range-max 80

#name='servicevm_internal_port_security_group'
#service_tenant=$(keystone tenant-list | grep -w services  | cut -d'|' -f2)
#echo $service_tenant
#security_group=$(neutron security-group-create --tenant-id $service_tenant  $name |grep ' id ' | awk '{print $2}')
#echo $security_group
#neutron security-group-rule-create $security_group --tenant-id $service_tenant --direction egress --ethertype IPv4 
