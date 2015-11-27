#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo "token: "$TOKEN
#TOKEN="3ac866661cac476a903af4b6f90c9773"
#curl -i 'http://127.0.0.1:9696/v2.0/vm/service-instances.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
#    "service_instance": {
#        "service_type_id": "4dac6778-f3a7-45ab-a0aa-7f8944f0e620",
#        "devices": [
#            "ef0d9125-660b-4bd4-ad28-7a8feff19128"
#        ],
#        "attributes": {
#            "external_gateway": [
#                {
#                    "floatingip_id": "fab4cd30-5cef-411a-809d-e0f1782b210a",
#                    "fixed_port_id": "362c09a4-1819-455d-9194-09ac62bd8856"
#                }
#            ],
#            "floatingip": [
#                {
#                    "fixed_port_id": "f39d8df6-a362-4f0e-a45b-5b82c299e8b8",
#                    "floatingip_id": "99c7c02b-f5d3-4dbb-812b-efcf55890377"
#                }
#            ]
#        },
#        "mgmt_driver": "agent_rpc"
#    }
#}'


curl -i 'http://127.0.0.1:9696/v2.0/vm/service-instances.json' -X POST -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "service_instance": {
        "service_type_id": "67cc00ea-8523-4473-afca-cad26017a01d",
        "devices": [
            "093d1064-63a8-4faa-ab3d-3f6054c16f55"
        ],
        "attributes": {
            "floatingip": [
                {
                    "fixed_port_id": "9547dc71-de3d-43ca-b98f-ff74b74972c8",
                    "floatingip_id": "cac138fc-56df-41b0-a3dd-52bf87fb238f"
                }
            ],
            "external_gateway": [
                {
                    "fixed_port_id": "b1ac5087-7be6-4438-82de-a98b6520f83e",
                    "floatingip_id": "36844d19-5220-43d5-a763-5a81f52d89e2"
                }
            ]
        },
        "mgmt_driver": "agent_rpc"
    }
}'
