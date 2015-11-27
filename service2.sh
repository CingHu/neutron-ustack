#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo "token: "$TOKEN
curl -i 'http://127.0.0.1:9696/v2.0/vm/service-instances.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "service_instance": {
        "service_type_id": "67cc00ea-8523-4473-afca-cad26017a01d",
        "devices": [
            "0e750f89-218e-4d65-bef0-07273c22b95b"
        ],
        "attributes": {
            "floatingip": [
                {
                    "fixed_port_id": "be0d6253-2eb6-43cb-a253-828cabef976b",
                    "floatingip_id": "8b26a75a-1c3c-4c61-9da0-5592ada8f693"
                }
            ],
            "external_gateway": [
                {
                    "fixed_port_id": "dea68438-61cd-4bdf-b734-585375980a27",
                    "floatingip_id": "72dcbb15-1b6b-48ac-b1a2-1b0e2398c900"
                }
            ]
        },
        "mgmt_driver": "agent_rpc"
    }
}'
