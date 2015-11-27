#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo $TOKEN
#TOKEN="3ac866661cac476a903af4b6f90c9773"
curl -i 'http://127.0.0.1:9696/v2.0/vm/device-templates.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "device_template": {
        "attributes": {
            "flavor": "47f7e3bd-a6ff-41bb-ad62-5b9494126f0c",
            "image": "7a217be2-fd4c-4216-a3a8-2b31091baed1",
            "availability_zone":"nova:server-68",
            "networks": [
                {
                    "network_id": "3b3ac177-6e9f-4f8f-836d-002611eff90e"
                }
            ]
        },
        "service_types": [  
            { 
                "service_type": "VROUTER"
            } 
        ],
        "device_driver": "hillstone",
        "name": "hillstone",
        "mgmt_driver": "agent_rpc",
        "infra_driver": "nova",
        "shared":"True"
    }
}'

#            "image": "566cd68d-ba01-41a1-8481-04d8fec02bb1",
#            "image": "7a217be2-fd4c-4216-a3a8-2b31091baed1",
