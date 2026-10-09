"""NX HTTPS transport: explicit operations, reviewed origin, lazy secret injection.

No login, key generation, redirects, proxies, automatic retries or secret persistence.
"""
from dataclasses import dataclass
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from urllib.parse import urlencode, urlsplit

from webapp.timetracker_nx import FIELDS, MESSAGES, NXError, ResultUnknown, Settings, identifier


@dataclass(frozen=True, repr=False)
class HTTPProfile:
    settings: Settings
    user_id: str
    auth_mode: str
    credential_env: str

    @classmethod
    def from_dict(cls, value):
        required={'version','api_base','allowed_host','user_id','timezone','granularity',
                  'required_categories','auth_mode','credential_env','company_approved'}
        try:
            if (not isinstance(value,dict) or set(value)!=required or type(value['version']) is not int or value['version']!=1
                    or value['company_approved'] is not True or type(value['granularity']) is not int
                    or not isinstance(value['required_categories'],list)):
                raise ValueError()
            settings=Settings(value['api_base'],value['timezone'],value['granularity'],value['required_categories'])
            if value['allowed_host']!=settings.host or not re.fullmatch(r'[0-9]+',value['user_id']):
                raise ValueError()
            if value['auth_mode'] not in {'api_key','bearer'} or not re.fullmatch(r'MOKVIA_NX_[A-Z0-9_]{1,64}',value['credential_env']):
                raise ValueError()
            return cls(settings,value['user_id'],value['auth_mode'],value['credential_env'])
        except (ValueError,TypeError,KeyError):
            raise NXError('configuration_invalid') from None

    def configured(self):
        # Presence only; no credentials are loaded by startup or status.
        return bool(os.environ.get(self.credential_env))

    def credential(self):
        value=os.environ.get(self.credential_env)
        if not value:
            raise NXError('credential_missing')
        if len(value)>8192 or any(ord(c)<33 or ord(c)>126 for c in value):
            raise NXError('credential_invalid')
        return value


def load_integration_configuration(path):
    """Read only non-secret configuration. Never contact NX or load a key here."""
    if path is None:
        return {},{}
    try:
        path=Path(path)
        if (not path.is_absolute() or any(p.is_symlink() for p in [path,*path.parents])
                or not path.is_file() or path.stat().st_size>65536
                or (os.name!='nt' and path.stat().st_mode & 0o022
                    and not os.statvfs(path).f_flag & os.ST_RDONLY)):
            raise ValueError()
        profile=HTTPProfile.from_dict(json.loads(path.read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError):
        raise NXError('configuration_invalid') from None
    settings=profile.settings
    return {'timetracker_nx':{'api_base':settings.api_base,'timezone':str(settings.timezone),
            'granularity':settings.granularity,'required_categories':settings.required_categories}},\
           {'timetracker_nx_transport':NXHTTPTransport(profile)}


class NXHTTPTransport:
    """Dedicated allowlist transport. HTTPConnection injection is for TLS doubles."""
    synthetic_only=False

    def __init__(self,profile,*,connection_factory=None,ssl_context=None,timeout=15):
        self.profile=profile
        self._context=ssl_context or ssl.create_default_context()
        if self._context.verify_mode!=ssl.CERT_REQUIRED or not self._context.check_hostname or not 0<timeout<=30:
            raise NXError('configuration_invalid')
        self._connection=connection_factory or http.client.HTTPSConnection
        self._timeout=timeout
        self._verified_key=None

    def configured(self):
        return self.profile.configured()

    def request(self,method,path,query=None,body=None):
        # Only me + this configured person's time entries. No arbitrary URL or user.
        prefix='/system/users/'+self.profile.user_id+'/timeEntries'
        valid=(method=='GET' and path=='/system/users/me' and query is None and body is None)
        valid=valid or (method=='GET' and path==prefix and body is None and isinstance(query,dict)
                       and set(query)=={'startDate','finishDate','limit','offset','orderby'})
        valid=valid or (method=='POST' and path==prefix and query is None and isinstance(body,dict)
                       and {'workItemId','startTime','finishTime'}<=set(body)<=set(FIELDS))
        valid=valid or (method=='PUT' and isinstance(path,str) and path.startswith(prefix+'/')
                       and re.fullmatch(r'[0-9]+',path[len(prefix)+1:]) and query is None
                       and isinstance(body,dict) and bool(body) and set(body)<=set(FIELDS)-{'workItemId'})
        if not valid:
            raise NXError('invalid_data')
        if query is not None:
            if (any(not isinstance(query[k],str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',query[k]) for k in ('startDate','finishDate'))
                    or type(query['limit']) is not int or not 1<=query['limit']<=100
                    or type(query['offset']) is not int or query['offset']<0 or query['orderby']!='id asc'):
                raise NXError('invalid_data')
        credential=self.profile.credential()
        key=hashlib.sha256(credential.encode('ascii')).digest()
        if path!='/system/users/me' and self._verified_key!=key:
            raise NXError('identity_required')
        header='X-TT-ApiKey' if self.profile.auth_mode=='api_key' else 'Authorization'
        headers={'Accept':'application/json','Content-Type':'application/json; charset=utf-8',
                 header:credential if header=='X-TT-ApiKey' else 'Bearer '+credential}
        target=urlsplit(self.profile.settings.api_base).path+path
        if query is not None:
            target+='?'+urlencode(query)
        data=json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8') if body is not None else None
        mutating=method in {'POST','PUT'}
        connection=None
        try:
            # HTTPSConnection does not implement redirects and does not use env proxies.
            connection=self._connection(self.profile.settings.host,443,context=self._context,timeout=self._timeout)
            connection.request(method,target,body=data,headers=headers)
            response=connection.getresponse()
            status=response.status
            raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:
                raise ValueError()
            if 300<=status<400 or status==429 or status>=500:
                if mutating:
                    raise ResultUnknown()
                raise NXError('redirect_blocked' if 300<=status<400 else 'rate_limited' if status==429 else 'server_unavailable')
            if status in (401,403):
                self._verified_key=None
            if status>=400:
                code='authentication_required' if status==401 else 'OperationDenied' if status==403 else 'request_rejected'
                rejected=status in (401,403)
                try:
                    errors=json.loads(raw)
                    if (response.getheader('Content-Type','').split(';')[0].strip().lower()=='application/json'
                            and isinstance(errors,list) and len(errors)==1 and isinstance(errors[0],dict)
                            and errors[0].get('code') in {c for c in MESSAGES if c[0].isupper()}):
                        code=errors[0]['code'] if status not in (401,403) else code
                        rejected=True
                except (ValueError,TypeError):
                    pass
                if mutating and (not rejected or status in (408,425)):
                    raise ResultUnknown()
                raise NXError(code)
            if not 200<=status<300:
                raise ValueError()
            if not raw and method=='PUT':
                return None
            if response.getheader('Content-Type','').split(';')[0].strip().lower()!='application/json':
                raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):
                raise ValueError()
            if path=='/system/users/me':
                self._verified_key=None
                if identifier(value.get('id'))!=self.profile.user_id:
                    raise NXError('not_self')
                self._verified_key=key
            return value
        except (NXError,ResultUnknown):
            raise
        except Exception:
            if mutating:
                raise ResultUnknown() from None
            raise NXError('transport_failed') from None
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()
            # Neither credentials nor response text become state, receipts or logs.
            credential=None
            headers.clear()
