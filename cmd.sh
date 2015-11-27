{'files': {'/etc/neutron/neutron.ini': '[servicevm]\ntopic = servicevm_agent\ndevice_id = 6b2cbdcd-a21f-4c16-9c93-1427b0b23409\n'}}


create(name, image, flavor, meta=None, files=None, reservation_id=None, min_count=None, max_count=None, security_groups=None, userdata=None, key_name=None, availability_zone=None, block_device_mapping=None, block_device_mapping_v2=None, nics=None, scheduler_hints=None, config_drive=None, disk_config=None, **kwargs)



nova boot testhu --availability-zone nova --image 7a217be2-fd4c-4216-a3a8-2b31091baed1 --flavor 47f7e3bd-a6ff-41bb-ad62-5b9494126f0c --nic net-id=3b3ac177-6e9f-4f8f-836d-002611eff90e --nic net-id=687856aa-3401-4771-aca3-fa5d4f1d6f56 --files '/etc/neutron/neutron.ini': '[servicevm]\ntopic = servicevm_agent\ndevice_id = 6b2cbdcd-a21f-4c16-9c93-1427b0b23409\n'

#delete devices
id=$(neutron device-list | grep -v id | awk -F'|'  '{print $5}' )
for i in $id;do neutron device-delete $i;done
