#!/bin/bash

fips=$(neutron floatingip-list|grep 0ae17794-9f5f-4270-939a-a2b4efe805d2| awk ' { print $2} ')
