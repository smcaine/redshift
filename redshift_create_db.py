#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['stableinterface'],
                    'supported_by': 'community'}


DOCUMENTATION = '''
---
module: redshift_create_db
short_description: Add or remove Redshift databases from a remote host. This is based on the Postgres Ansible Module
description:
   - Add or remove Redshift databases from a remote host.
version_added: "0.1"
options:
  name:
    description:
      - name of the database to add or remove
    required: true
    aliases: [ db ]
  state:
    description: |
        The database state. present implies that the database should be created if necessary.
        absent implies that the database should be removed if present.
    default: present
    choices: [ "present", "absent" ]
    description:
      - File to back up or restore from. Used when state is "dump" or "restore"
  maintenance_db:
    version_added: "0.1"
    description:
      - The value specifies the initial database (which is also called as maintenance DB) that Ansible connects to.
    default: postgres
author: "Stuart Caine"
'''

EXAMPLES = '''
# Create a new database with name "acme"
- redshift_create_db:
    name: acme

# Create a new database with name "acme" and specific encoding and locale
# settings.
'''

import os
import pipes
import subprocess
import traceback

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    HAS_PSYCOPG2 = False
else:
    HAS_PSYCOPG2 = True

import ansible.module_utils.postgres as pgutils
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.database import SQLParseError, pg_quote_identifier
from ansible.module_utils.six import iteritems
from ansible.module_utils._text import to_native


class NotSupportedError(Exception):
    pass


# ===========================================
# Redshift module specific support methods.
#

def db_exists(cursor, db):
    query = "SELECT * FROM pg_database WHERE datname=%(db)s"
    cursor.execute(query, {'db': db})
    return cursor.rowcount == 1


def db_delete(cursor, db):
    if db_exists(cursor, db):
        query = "DROP DATABASE %s"
        cursor.execute(query)
        return True
    else:
        return False

def db_create(cursor, db):
    if not db_exists(cursor, db):
        query = "CREATE DATABASE %s" % pg_quote_identifier(db, 'database')
        cursor.execute(query)
        return True
    else:
        return False

def db_matches(cursor, db):
    if not db_exists(cursor, db):
        return False
    else:
        return True

def login_flags(db, host, port, user, db_prefix=True):
    """
    returns a list of connection argument strings each prefixed
    with a space and quoted where necessary to later be combined
    in a single shell string with `"".join(rv)`
    db_prefix determines if "--dbname" is prefixed to the db argument,
    since the argument was introduced in 9.3.
    """
    flags = []
    if db:
        if db_prefix:
            flags.append(' --dbname={0}'.format(pipes.quote(db)))
        else:
            flags.append(' {0}'.format(pipes.quote(db)))
    if host:
        flags.append(' --host={0}'.format(host))
    if port:
        flags.append(' --port={0}'.format(port))
    if user:
        flags.append(' --username={0}'.format(user))
    return flags


def do_with_password(module, cmd, password):
    env = {}
    if password:
        env = {"PGPASSWORD": password}
    rc, stderr, stdout = module.run_command(cmd, use_unsafe_shell=True, environ_update=env)
    return rc, stderr, stdout, cmd

# ===========================================
# Module execution.
#


def main():
    argument_spec = pgutils.postgres_common_argument_spec()
    argument_spec.update(dict(
        db=dict(required=True, aliases=['name']),
        state=dict(default="present", choices=["absent", "present"]),
        maintenance_db=dict(default="postgres"),
    ))

    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True
    )

    if not HAS_PSYCOPG2:
        module.fail_json(msg="the python psycopg2 module is required")

    db = module.params["db"]
    state = module.params["state"]
    changed = False
    maintenance_db = module.params['maintenance_db']

    # To use defaults values, keyword arguments must be absent, so
    # check which values are empty and don't include in the **kw
    # dictionary
    params_map = {
        "login_host": "host",
        "login_user": "user",
        "login_password": "password",
        "port": "port",
        "ssl_mode": "sslmode",
        "ssl_rootcert": "sslrootcert"
    }
    kw = dict((params_map[k], v) for (k, v) in iteritems(module.params)
              if k in params_map and v != '' and v is not None)

    # If a login_unix_socket is specified, incorporate it here.
    is_localhost = "host" not in kw or kw["host"] == "" or kw["host"] == "localhost"

    try:
        pgutils.ensure_libs(sslrootcert=module.params.get('ssl_rootcert'))
        db_connection = psycopg2.connect(database=maintenance_db, **kw)

        # Enable autocommit so we can create databases
        if psycopg2.__version__ >= '2.4.2':
            db_connection.autocommit = True
        else:
            db_connection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = db_connection.cursor(cursor_factory=psycopg2.extras.DictCursor)

    except pgutils.LibraryError as e:
        module.fail_json(msg="unable to connect to database: {0}".format(to_native(e)), exception=traceback.format_exc())

    except TypeError as e:
        if 'sslrootcert' in e.args[0]:
            module.fail_json(msg='Postgresql server must be at least version 8.4 to support sslrootcert. Exception: {0}'.format(to_native(e)),
                             exception=traceback.format_exc())
        module.fail_json(msg="unable to connect to database: %s" % to_native(e), exception=traceback.format_exc())

    except Exception as e:
        module.fail_json(msg="unable to connect to database: %s" % to_native(e), exception=traceback.format_exc())

    try:
        if module.check_mode:
            if state == "absent":
                changed = db_exists(cursor, db)
            elif state == "present":
                changed = not db_matches(cursor, db)
            module.exit_json(changed=changed, db=db)

        if state == "absent":
            try:
                changed = db_delete(cursor, db)
            except SQLParseError as e:
                module.fail_json(msg=to_native(e), exception=traceback.format_exc())

        elif state == "present":
            try:
                changed = db_create(cursor, db)
            except SQLParseError as e:
                module.fail_json(msg=to_native(e), exception=traceback.format_exc())

    except NotSupportedError as e:
        module.fail_json(msg=to_native(e), exception=traceback.format_exc())
    except SystemExit:
        # Avoid catching this on Python 2.4
        raise
    except Exception as e:
        module.fail_json(msg="Database query failed: %s" % to_native(e), exception=traceback.format_exc())

    module.exit_json(changed=changed, db=db)


if __name__ == '__main__':
    main()