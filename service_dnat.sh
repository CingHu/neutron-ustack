#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo $TOKEN
#TOKEN="3ac866661cac476a903af4b6f90c9773"
curl -i 'http://127.0.0.1:9696/v2.0/vm/service-instances.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "service_instance": {
        "service_type_id": "e9d1fc95-c684-4fc3-bd6b-c50cb595f07f",
        "devices": [
            "78f3f855-11c3-4536-a84a-70f64e8cbbea"
        ],
        "attributes": {
            "dnat_service": [
                {
                    "floatingip_id": "239c858a-eabb-4988-bce1-154f4f64b5b0",
                    "to_ip": "1.2.3.4"
                },
                {
                    "floatingip_id": "239c858a-eabb-4988-bce1-154f4f64b5b0",
                    "to_ip": "5.6.7.8"
                }
            ],
            "floatingip_service": [
                {
                    "fixed_port_id": "23518936-85ee-4def-b3c4-13c44e14dfd4",
                    "floatingip_id": "94abe1d8-0a76-4613-806a-c10d7848c100"
                }
            ]
        },
        "mgmt_driver": "agent_rpc"
    }
}'
