#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo $TOKEN
#TOKEN="3ac866661cac476a903af4b6f90c9773"
NETWORK="91c26a02-4544-405d-b258-115734a5c33a"
curl -i 'http://127.0.0.1:9696/v2.0/vm/device-templates.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "device_template": {
        "attributes": {
            "flavor": "47f7e3bd-a6ff-41bb-ad62-5b9494126f0c",
            "image": "7a217be2-fd4c-4216-a3a8-2b31091baed1",
            "availability_zone":"nova",
            "networks": [
                {
                    "net-id": "3b3ac177-6e9f-4f8f-836d-002611eff90e"
                }
            ]
        },
        "service_types": [  
            { 
                "service_type": "IPS"
            },
            { 
                "service_type": "VROUTER"
            } 
        ],
        "device_driver": "hillstone",
        "mgmt_driver": "agent_rpc",
        "infra_driver": "nova",
        "shared":"True"
    }
}'


curl -i 'http://127.0.0.1:9696/v2.0/vm/devices.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "device": {
        "template_id": "fef07173-a93f-412f-9ba6-cb3f02011fc6",
        "attributes": {
            "subnets": [{"subnet-id":"50002aec-e256-4bfe-8635-1771f38572d9","subnet-id":"aaad0523-56a0-415e-ab28-8b6803e61cbf"}]
        }
   }
}'
