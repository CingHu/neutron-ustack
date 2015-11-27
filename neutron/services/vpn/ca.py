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
#
# @author: hu xining, UnitedStack Inc.

import os
import sys
import zipfile
import glob

from oslo.config import cfg
import configobj

from neutron.services.vpn.common import constants as vpn_connstants
from neutron.agent.linux import utils
from neutron.agent.linux import ip_lib
from neutron.openstack.common import log as logging
from neutron.openstack.common import jsonutils
from neutron.extensions import openvpn
from neutron.agent.common import config

from neutron import context


LOG = logging.getLogger(__name__)

CONFIG_PATH = os.path.dirname(__file__)

OPENVPN_NEUTRON_SERVER_OPTS = [
    cfg.StrOpt('ca_file', default='/var/lib/neutron/openvpn/ca/ca.crt',
                help=_('ca file path for server and client')),

    cfg.StrOpt('ca_key', default='/var/lib/neutron/openvpn/ca/ca.key',
                help=_('ca key file path for server and client')),

    cfg.StrOpt('dh_file', default='/var/lib/neutron/openvpn/ca/dh.pem',
                help=_('dh param path for server')),

    cfg.StrOpt('ca_path', default='/var/lib/neutron/openvpn/ca',
                help=_('ca absolute path for server and client')),

    cfg.StrOpt('server_path', default='/var/lib/neutron/openvpn/server',
                help=_('server crt and key path')),

    cfg.StrOpt('client_path', default='/var/lib/neutron/openvpn/client',
                help=_('client crt and key path')),

    cfg.StrOpt('openssl_conf',
               default=os.path.join(
               CONFIG_PATH,
               'config/openssl.cnf'),
                help=_('the path of openssl config')),

    cfg.StrOpt('client_template',
               default=os.path.join(
               CONFIG_PATH,
               'config/openvpn.client.template'),
                help=_('the template file of client')),
]

cfg.CONF.register_opts(OPENVPN_NEUTRON_SERVER_OPTS,'openvpn')

def get_file_name(id, server=True):
    if server:
        server_mid = (vpn_connstants.CA_SERVER_PREFIX + id)[:vpn_connstants.FILE_NAME_LEN]
        file_name = os.path.join(cfg.CONF.openvpn.server_path, server_mid)
    else:
        client_mid = (vpn_connstants.CA_CLIENT_PREFIX + id)[:vpn_connstants.FILE_NAME_LEN]
        file_name = os.path.join(cfg.CONF.openvpn.client_path, client_mid)

    return file_name

def remove_file(name):
    if os.path.exists(name):
        os.remove(name)

def read_file(name):
    if not os.path.exists(name):
        raise openvpn.FileNotFound(name=name)

    with open(name, 'rb') as f:
        return f.read()

def write_file(name, contents):
    remove_file(name)
    with open(name,'wb') as f:
        if type(contents) == list:
            for line in contents:
                f.write(line)
        else:
            f.write(contents)

def write_base64_file(name, contents):
    remove_file(name)
    with open(name,'wb') as f:
        f.write(contents.decode('base64'))

def write_zip_file(id, contents):
    prefix = get_file_name(id, server=False)
    write_base64_file(prefix+'.zip', contents)

def file_is_exists(id):
    name = get_file_name(id)
    r = True if os.path.isfile(name+'.key') and os.path.isfile(name+'.crt') else False
    return  r

class VPNEnv(object):
    '''generate server.crt, server.key, client.crt and client.key in server node'''

    def __init__(self):
        self.envs = {}

    def add_env(self, k, v):
        self.envs[k] = v

    def remove_env(self, k):
        del self.envs[k]

    def load_env(self):
        for k, v in self.envs.iteritems():
            os.environ[k] = str(v)

    def replace_env(self, k, v):
        self.remove_env(k)
        self.add_env(k, v)

    def debug_env(self):
        for k, v in self.envs.iteritems():
            LOG.info("openvpn ca info, key=%s, value=%s" % (k, v))

class OpenVPNCA(VPNEnv):

    def __init__(self):
        super(OpenVPNCA, self).__init__()
        config.register_root_helper(cfg.CONF)
        self.root_helper = cfg.CONF.AGENT.root_helper
        self.ca_path = cfg.CONF.openvpn.ca_path

    def set_openvpn_env(self):
        openvpn_cons = ['OPENSSL', 'PKCS11TOOL', 'GREP', 'PKCS11_MODULE_PATH', 'PKCS11_PIN', \
                        'KEY_SIZE', 'CA_EXPIRE', 'KEY_EXPIRE', 'KEY_COUNTRY', 'KEY_PROVINCE', \
                        'KEY_CITY', 'KEY_ORG', 'KEY_EMAIL', 'KEY_OU', 'KEY_NAME']

        self.add_env('KEY_DIR', self.ca_path)
        for openvpn_con in openvpn_cons:
            self.add_env(openvpn_con, getattr(vpn_connstants, openvpn_con))

        self.load_env()
        self.debug_env()

    def get_file_name(self, id, server=True):
        if server:
            server_mid = (vpn_connstants.CA_SERVER_PREFIX + id)[:vpn_connstants.FILE_NAME_LEN]
            file_name = os.path.join(cfg.CONF.openvpn.server_path, server_mid)
        else:
            client_mid = (vpn_connstants.CA_CLIENT_PREFIX + id)[:vpn_connstants.FILE_NAME_LEN]
            file_name = os.path.join(cfg.CONF.openvpn.client_path, client_mid)

        return file_name

    def gen_dh(self):
       dh_dest_file = os.path.join(cfg.CONF.openvpn.ca_path, "dh.pem")
       cmd = ['openssl','dhparam','-out', dh_dest_file, cfg.CONF.openvpn.KEY_SIZE]
       result = utils.execute(cmd, root_helper=self.root_helper)
       LOG.info("openvpn generate dh of server, result:%s" % result)

    def gen_ta_key(self, id):
       file_prefix = get_file_name(id, server=False)
       file_name = file_prefix + 'ta.key'
       remove_file(file_name)
       cmd = ['openvpn','--genkey','--secret', file_name]
       result = utils.execute(cmd, root_helper = self.root_helper)
       LOG.info("openvpn generate ta_key of server, result:%s" % result)
       cmd = ['chmod', '777', file_name]
       result = utils.execute(cmd, root_helper = self.root_helper)
       return read_file(file_name)

    def gen_root_ca(self, ca_name):
       ca_key_file = os.path.join(self.ca_path, key_name )
       cmd = ['openssl',
              'req',
              '-batch',
              '-days', vpn_connstants.KEY_EXPIRE,
              '-nodes',
              '-new', '-newkey', 'rsa:' + str(vpn_connstants.KEY_SIZE),
              '-x509',
              '-keyout', ca_key_file + '.key',
              '-out', ca_key_file + '.crt',
              '-config', cfg.CONF.openvpn.openssl_conf]
       result = utils.execute(cmd, root_helper=self.root_helper)
       LOG.info("openvpn generate ca of server, result:%s" % result)

    def gen_server_ca(self, id):
        file_prefix = get_file_name(id)
        file_key = file_prefix + '.key'
        file_csr = file_prefix + '.csr'
        remove_file(file_key)
        remove_file(file_csr)
        cmd = ['openssl',
               'req',
               '-batch',
               '-nodes',
               '-new',
               '-newkey', 'rsa:' + str(vpn_connstants.KEY_SIZE),
               '-keyout', file_key,
               '-out', file_csr,
               '-extensions', 'server',
               '-config', cfg.CONF.openvpn.openssl_conf]

        result = utils.execute(cmd, root_helper=self.root_helper)
        LOG.info("openvpn generate key of server, result:%s" % result)

        file_crt = file_prefix + '.crt'
        remove_file(file_crt)
        cmd = ['openssl',
               'ca',
               '-batch',
               '-days', vpn_connstants.KEY_EXPIRE,
               '-out', file_crt,
               '-in', file_csr,
               '-extensions', 'server',
               '-config', cfg.CONF.openvpn.openssl_conf]

        result = utils.execute(cmd, root_helper=self.root_helper)
        LOG.info("openvpn generate crt of server, result:%s" % result)

    def gen_client_ca(self, id):
        file_prefix = get_file_name(id, server = False)
        file_key = file_prefix + '.key'
        file_csr = file_prefix + '.csr'
        remove_file(file_key)
        remove_file(file_csr)
        cmd = ['openssl',
               'req',
               '-batch',
               '-nodes',
               '-new',
               '-newkey', 'rsa:' + str(vpn_connstants.KEY_SIZE),
               '-keyout', file_key,
               '-out', file_csr,
               '-config', cfg.CONF.openvpn.openssl_conf]

        result = utils.execute(cmd, root_helper=self.root_helper)
        LOG.info("openvpn generate key of client, result:%s" % result)

        file_crt = file_prefix + '.crt'
        remove_file(file_crt)
        cmd = ['openssl',
               'ca',
               '-batch',
               '-days', vpn_connstants.KEY_EXPIRE,
               '-out', file_crt,
               '-in', file_csr,
               '-config', cfg.CONF.openvpn.openssl_conf]

        result = utils.execute(cmd, root_helper=self.root_helper)
        LOG.info("openvpn generate crt of cient, result:%s" % result)
        remove_file(file_csr)


class OpenVPNFile():

    def __init__(self, openvpnservice):
       self.openvpnservice = openvpnservice
       self.file_prefix = get_file_name(openvpnservice['id'], server=False)
       self.client_conf = cfg.CONF.openvpn.client_template
       self.ca_file = cfg.CONF.openvpn.ca_file
       if os.path.exists(self.client_conf):
           self.config = configobj.ConfigObj(self.client_conf)
       else:
           LOG.error("%s file is not exists" % self.client_conf)
           raise


    def generate_zip_file(self):
        if 'zip_file' in self.openvpnservice:
            write_base64_file(self.file_prefix+'.zip', self.openvpnservice['zip_file'])
            za = zipfile.ZipFile(self.file_prefix + '.zip', 'r')
            za.extractall(cfg.CONF.openvpn.client_path)
            za.close()

        LOG.info("generate zip file %s" % self.openvpnservice)
        self._generate_config_file()

        LOG.info("zip prefix file name %s" % self.file_prefix)
        self._generate_zip()

        return read_file(self.file_prefix+'.zip')

    def _generate_config_file(self):
        os_versions = ["windows", "linux", "mac", "android"]
        for os_type in os_versions:
            sectionname = os_type + ' conf'
            if os_type == "windows":
                write_file_name = self.file_prefix+'.'+os_type+'.ovpn'
            else:
                write_file_name = self.file_prefix+'.'+os_type+'.conf'

            remove_file(write_file_name)
            LOG.info("write file name, %s" % write_file_name)
            f = open(write_file_name, 'w')
            section=self.config[sectionname]
            for var in section:
               LOG.info("%s,%s"% (var, section[var]))
               if var == 'ca':
                   f.write(section[var].replace('cacertfile',  'ca.crt') + '\n')
               elif var == 'cert':
                   f.write(section[var].replace('clientcertfile', \
                              os.path.basename(self.file_prefix) + '.crt') + '\n')
               elif var == 'key':
                   f.write(section[var].replace('clientkeyfile', \
                              os.path.basename(self.file_prefix) + '.key') + '\n')
               elif var == 'tls':
                   f.write(section[var].replace('tlsauthfile',\
                              os.path.basename(self.file_prefix) + 'ta.key') + '\n')
               elif var == 'ip':
                   if 'ex_gw_ip' in self.openvpnservice and \
                            self.openvpnservice['ex_gw_ip'] is not None:
                       f.write('remote '+self.openvpnservice['ex_gw_ip']+'\n')
                   else:
                       f.write('remote '+'<your-server-ip>'+'\n')
               elif var == 'port':
                   if 'port' in self.openvpnservice:
                       f.write('port '+str(self.openvpnservice['port'])+'\n')
                   else:
                       f.write(section[var] + '\n')
               elif var == 'proto':
                   if 'protocol' in self.openvpnservice:
                       f.write('proto '+self.openvpnservice['protocol']+'\n')
                   else:
                       f.write(section[var] + '\n')
               else:
                   f.write(section[var] + '\n')

            if os_type == 'android':
                fp = open ( self.ca_file, 'r' )
                f.write('\n' + "<ca>" + '\n' + fp.read() + "</ca>" + '\n')
                fp.close ()
                fp = open ( self.file_prefix + '.crt', 'r' )
                f.write('\n' + "<cert>" + '\n' + fp.read() + "</cert>" + '\n')
                fp.close ()
                fp = open ( self.file_prefix  + '.key', 'r' )
                f.write('\n' + "<key>" + '\n' + fp.read() + "</key>" + '\n')
                fp = open ( self.file_prefix  + 'ta.key', 'r' )
                f.write('\n' + "<tls-auth>" + '\n' + fp.read() + "</tls-auth> 1" + '\n')
                fp.close ()

            f.close()

    def _generate_zip(self):
        name = self.file_prefix + '.zip'
        remove_file(name)
        z = zipfile.ZipFile(name, 'w')
        for name in glob.glob(self.file_prefix+'*'):
            if not name == self.file_prefix + ".zip":
                z.write(name, os.path.basename(name), zipfile.ZIP_DEFLATED)

        #add ca file
        z.write(self.ca_file, os.path.basename(self.ca_file), zipfile.ZIP_DEFLATED)
        z.close()

    def read_zip_file(self):
        z = zipfile.ZipFile(self.file_prefix + '.zip', 'r')
        return z

    def close_zip_file(self, zip_p):
        zip_p.close()

    def remove_all_file(self):
        #remove cert, key, config, etc.
        for name in glob.glob(self.file_prefix+'*'):
                os.remove(name)

class OpenVPNDBDrv(OpenVPNCA):
    def __init__(self):
        super(OpenVPNDBDrv, self).__init__()

    def generate_server_ca(self, openvpn_service):
        self.set_openvpn_env()
        name = get_file_name(openvpn_service['id']) + 'ta.key'
        write_file(name, openvpn_service['ta_key'])
        self.gen_server_ca(openvpn_service['id'])

    def generate_client_ca(self, openvpn_id):
        contents = {}
        self.set_openvpn_env()
        contents['ta_key'] = self.gen_ta_key(openvpn_id)
        self.gen_client_ca(openvpn_id)
        return contents
