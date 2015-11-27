#!/bin/bash
TOKEN=$(keystone token-get | grep -w id  | awk -F"|" '{print $3}')
echo "token: "$TOKEN
#TOKEN="3ac866661cac476a903af4b6f90c9773"
SVC_ID=8fe64de4-9037-4ad2-bfd1-8030f7f9cf06
curl -i "http://127.0.0.1:9696/v2.0/vm/service-instances/${SVC_ID}" -X PUT -H "X-Auth-Token:${TOKEN}" -H "Content-Type: application/json" -H "Accept: application/json" -H "User-Agent: python-neutronclient" -d '{
    "service_instance": {
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
                    "floatingip_id": null
                }
            ]
        }
    }
}'
                    #"floatingip_id": null
                    #"floatingip_id": "36844d19-5220-43d5-a763-5a81f52d89e2"
