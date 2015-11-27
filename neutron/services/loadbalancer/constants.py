# Copyright 2013 Mirantis, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

#FIXME(brandon-logan): change these to LB_ALGORITHM
LB_METHOD_ROUND_ROBIN = 'ROUND_ROBIN'
LB_METHOD_LEAST_CONNECTIONS = 'LEAST_CONNECTIONS'
LB_METHOD_SOURCE_IP = 'SOURCE_IP'
SUPPORTED_LB_ALGORITHMS = (LB_METHOD_LEAST_CONNECTIONS, LB_METHOD_ROUND_ROBIN,
                           LB_METHOD_SOURCE_IP)

PROTOCOL_TCP = 'TCP'
PROTOCOL_HTTP = 'HTTP'
PROTOCOL_HTTPS = 'HTTPS'
PROTOCOL_TERMINATED_HTTPS = 'TERMINATED_HTTPS'
#POOL_SUPPORTED_PROTOCOLS = (PROTOCOL_TCP, PROTOCOL_HTTPS, PROTOCOL_HTTP, PROTOCOL_TERMINATED_HTTPS)
POOL_SUPPORTED_PROTOCOLS = (PROTOCOL_TCP, PROTOCOL_HTTP)
LISTENER_SUPPORTED_PROTOCOLS = (PROTOCOL_TCP, PROTOCOL_HTTP)
#LISTENER_SUPPORTED_PROTOCOLS = (PROTOCOL_TCP, PROTOCOL_HTTPS, PROTOCOL_HTTP,
#                                PROTOCOL_TERMINATED_HTTPS)

LISTENER_POOL_COMPATIBLE_PROTOCOLS = (
    (PROTOCOL_TCP, PROTOCOL_TCP),
    (PROTOCOL_HTTP, PROTOCOL_HTTP),
    (PROTOCOL_HTTPS, PROTOCOL_HTTPS),
    (PROTOCOL_TERMINATED_HTTPS, PROTOCOL_HTTP))


HEALTH_MONITOR_PING = 'PING'
HEALTH_MONITOR_TCP = 'TCP'
HEALTH_MONITOR_HTTP = 'HTTP'
HEALTH_MONITOR_HTTPS = 'HTTPS'
SUPPORTED_HEALTH_MONITOR_TYPES = (HEALTH_MONITOR_HTTP, HEALTH_MONITOR_TCP)

HEALTH_MONITOR_HTTP_GET = 'GET'
HEALTH_MONITOR_HTTP_POST = 'POST'
HEALTH_MONITOR_HTTP_PUT = 'PUT'
HEALTH_MONITOR_HTTP_DELETE = 'DELETE'

SUPPORTED_HTTP_CHECK_METHOD = (HEALTH_MONITOR_HTTP_GET, HEALTH_MONITOR_HTTP_PUT,
                                  HEALTH_MONITOR_HTTP_POST, HEALTH_MONITOR_HTTP_DELETE)


SESSION_PERSISTENCE_SOURCE_IP = 'SOURCE_IP'
SESSION_PERSISTENCE_HTTP_COOKIE = 'HTTP_COOKIE'
SESSION_PERSISTENCE_APP_COOKIE = 'APP_COOKIE'
SUPPORTED_SP_TYPES = (SESSION_PERSISTENCE_SOURCE_IP,
                      SESSION_PERSISTENCE_HTTP_COOKIE,
                      SESSION_PERSISTENCE_APP_COOKIE)

L7_RULE_TYPE_HOST_NAME = 'HOST_NAME'
L7_RULE_TYPE_PATH = 'PATH'
L7_RULE_TYPE_FILE_TYPE = 'FILE_TYPE'
L7_RULE_TYPE_HEADER = 'HEADER'
L7_RULE_TYPE_COOKIE = 'COOKIE'
SUPPORTED_L7_RULE_TYPES = (L7_RULE_TYPE_HOST_NAME,
                           L7_RULE_TYPE_PATH)
                           #L7_RULE_TYPE_FILE_TYPE,
                           #L7_RULE_TYPE_HEADER,
                           #L7_RULE_TYPE_COOKIE)

L7_RULE_COMPARE_TYPE_REGEX = 'REGEX'
L7_RULE_COMPARE_TYPE_STARTS_WITH = 'STARTS_WITH'
L7_RULE_COMPARE_TYPE_ENDS_WITH = 'ENDS_WITH'
L7_RULE_COMPARE_TYPE_CONTAINS = 'CONTAINS'
L7_RULE_COMPARE_TYPE_EQUALS_TO = 'EQUALS_TO'
L7_RULE_COMPARE_TYPE_GREATER_THAN = 'GREATER_THAN'
L7_RULE_COMPARE_TYPE_LESS_THAN = 'LESS_THAN'
SUPPORTED_L7_RULE_COMPARE_TYPES = (L7_RULE_COMPARE_TYPE_REGEX,
                                   #L7_RULE_COMPARE_TYPE_STARTS_WITH,
                                   #L7_RULE_COMPARE_TYPE_ENDS_WITH,
                                   #L7_RULE_COMPARE_TYPE_CONTAINS,
                                   L7_RULE_COMPARE_TYPE_EQUALS_TO)
                                   #L7_RULE_COMPARE_TYPE_GREATER_THAN,
                                   #L7_RULE_COMPARE_TYPE_LESS_THAN)

L7_POLICY_ACTION_REJECT = 'REJECT'
L7_POLICY_ACTION_REDIRECT_TO_POOL = 'REDIRECT_TO_POOL'
L7_POLICY_ACTION_REDIRECT_TO_URL = 'REDIRECT_TO_URL'
SUPPORTED_L7_POLICY_ACTIONS = (L7_POLICY_ACTION_REJECT,
                               L7_POLICY_ACTION_REDIRECT_TO_POOL,
                               L7_POLICY_ACTION_REDIRECT_TO_URL)

STATS_ACTIVE_CONNECTIONS = 'active_connections'
STATS_MAX_CONNECTIONS = 'max_connections'
STATS_TOTAL_CONNECTIONS = 'total_connections'
STATS_CURRENT_SESSIONS = 'current_sessions'
STATS_MAX_SESSIONS = 'max_sessions'
STATS_TOTAL_SESSIONS = 'total_sessions'
STATS_IN_BYTES = 'bytes_in'
STATS_OUT_BYTES = 'bytes_out'
STATS_CONNECTION_ERRORS = 'connection_errors'
STATS_RESPONSE_ERRORS = 'response_errors'
STATS_STATUS = 'status'
STATS_HEALTH = 'health'
STATS_FAILED_CHECKS = 'failed_checks'