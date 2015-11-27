{
    'service_table_id': u'',
    'status': u'PENDING_CREATE',
    'name': u'',
    'tenant_id': u'cf4e0ef3beae47fc9023f0d970539dd2',
    'created_at': datetime.datetime(2015,8, 26,10,45,16),
    'service_type_id': u'e9d1fc95-c684-4fc3-bd6b-c50cb595f07f',
    'devices': {
        'status': u'ACTIVE',
        'instance_id': u'c9d0fc1d-e3af-4a59-a7d3-d951fcf93997',
        'name': u'',
        'tenant_id': u'cf4e0ef3beae47fc9023f0d970539dd2',
        'template_id': u'e3a58a58-9861-4ac1-b82b-120acb9239e4',
        'device_template': {
            'service_types': [
                {
                    'service_type': u'IPS',
                    'id': u'72e2106d-d0b1-4891-84c9-94b3dd82e710'
                },
                {
                    'service_type': u'VROUTER',
                    'id': u'e9d1fc95-c684-4fc3-bd6b-c50cb595f07f'
                }
            ],
            'description': u'',
            'tenant_id': u'cf4e0ef3beae47fc9023f0d970539dd2',
            'infra_driver': u'nova',
            'mgmt_driver': u'agent_rpc',
            'shared': True,
            'attributes': {
                u'flavor': u'47f7e3bd-a6ff-41bb-ad62-5b9494126f0c',
                u'networks': [
                    {
                        u'network_id': u'3b3ac177-6e9f-4f8f-836d-002611eff90e'
                    }
                ],
                u'image': u'7a217be2-fd4c-4216-a3a8-2b31091baed1',
                u'availability_zone': u'nova'
            },
            'device_driver': u'hillstone',
            'id': u'e3a58a58-9861-4ac1-b82b-120acb9239e4',
            'name': u''
        },
        'mgmt_url': {
            u'subnet_id': u'ef33b4d3-049f-4705-98f0-fad12d68f4b2',
            u'network_id': u'3b3ac177-6e9f-4f8f-836d-002611eff90e',
            u'ip_address': u'10.0.88.130',
            u'port_id': u'8c41daf1-b826-4401-be9e-68ba439dbf0a',
            u'mac_address': u'fa: 16: 3e: 6c: 05: c1'
        },
        'power_state': u'ACTIVE',
        'services': [
            u'0604fb75-fc04-44da-a299-edafbcab4c22',
            u'548e0198-0daa-4cf4-8de3-337563753716',
            u'abb45d0c-bf2f-44c2-833d-78fa80482e0b',
            u'e23f288d-b492-45d2-93b4-2b6ec1a3735a',
            u'e6e3babb-7b96-46c3-a7ba-76036457cd7a',
            u'e784fc50-5148-4112-8edf-f38b98e47cc0'
        ],
        'attributes': {
            u'fixed_ips': [
                {
                    u'subnet_id': u'50002aec-e256-4bfe-8635-1771f38572d9'
                },
                {
                    u'subnet_id': u'77fbe5b9-13cc-44f5-b37c-998683767319'
                }
            ]
        },
        'id': u'78f3f855-11c3-4536-a84a-70f64e8cbbea',
        'description': u''
    },
    'mgmt_driver': u'agent-rpc',
    'mgmt_url': {
        u'subnet_id': u'ef33b4d3-049f-4705-98f0-fad12d68f4b2',
        u'network_id': u'3b3ac177-6e9f-4f8f-836d-002611eff90e',
        u'ip_address': u'10.0.88.130',
        u'port_id': u'8c41daf1-b826-4401-be9e-68ba439dbf0a',
        u'mac_address': u'fa: 16: 3e: 6c: 05: c1'
    },
    'service_type': {
        'servicetype': u'VROUTER',
        'id': u'e9d1fc95-c684-4fc3-bd6b-c50cb595f07f',
        'template_id': u'e3a58a58-9861-4ac1-b82b-120acb9239e4'
    },
    'attributes': {
        u'dnat_service': [
            {
                u'floatingip_id': u'239c858a-eabb-4988-bce1-154f4f64b5b0',
                u'to_ip': u'1.2.3.4'
            },
            {
                u'floatingip_id': u'239c858a-eabb-4988-bce1-154f4f64b5b0',
                u'to_ip': u'5.6.7.8'
            }
        ],
        u'floatingip_service': [
            {
                u'floatingip_id': u'94abe1d8-0a76-4613-806a-c10d7848c100',
                u'fixed_port_id': u'23518936-85ee-4def-b3c4-13c44e14dfd4'
            }
        ]
    },
    'id': u'0604fb75-fc04-44da-a299-edafbcab4c22'
}
