"""Read-only Graph acquisition; caller supplies an already-authorized in-memory token."""
import json
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from webapp.integrations.external_events import ImportError, instant, window

BASE='https://graph.microsoft.com/v1.0'
SELECT='id,iCalUId,seriesMasterId,type,originalStart,subject,start,end,isAllDay,isCancelled'
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def _get(url,token):
    request=Request(url,headers={'Authorization':'Bearer '+token,'Prefer':'outlook.timezone="UTC", IdType="ImmutableId"'})
    with build_opener(NoRedirect()).open(request,timeout=15) as response:
        raw=response.read(8*1024*1024+1)
    if len(raw)>8*1024*1024:raise ImportError('response_too_large')
    return json.loads(raw)

def graph_time(value):
    if not isinstance(value,dict) or value.get('timeZone') != 'UTC':raise ImportError('graph_timezone')
    return instant(value['dateTime'] if value['dateTime'].endswith(('Z','+00:00')) else value['dateTime']+'Z')

class GraphSource:
    def __init__(self,scope,calendar,token_provider,*,get=_get):
        self.scope,self.calendar,self.token_provider,self.get=scope,calendar,token_provider,get
    def fetch(self,first,last):
        first,last=window(first,last);token=self.token_provider()
        if not isinstance(token,str) or not token or any(c in token for c in '\r\n'):raise ImportError('authorization_required')
        prefix=BASE+'/me/calendars/'+quote(self.calendar,safe='')
        url=prefix+'/calendarView?'+urlencode({'startDateTime':first,'endDateTime':last,'$select':SELECT,'$top':'200'})
        events=[];seen=set();masters={}
        while url:
            parsed=urlsplit(url)
            if parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com' or parsed.fragment or parsed.path!=urlsplit(prefix+'/calendarView').path or url in seen or len(seen)>=100:
                raise ImportError('unsafe_graph_page')
            seen.add(url);page=self.get(url,token)
            if not isinstance(page,dict) or not isinstance(page.get('value'),list):raise ImportError('graph_page')
            for row in page['value']:
                if len(events)>=5000:raise ImportError('too_many_events')
                uid=row.get('iCalUId');occurrence=''
                if row.get('type') in ('occurrence','exception'):
                    master=row.get('seriesMasterId')
                    if not master or not row.get('originalStart'):raise ImportError('recurrence_identity_missing')
                    if master not in masters:
                        masters[master]=self.get(BASE+'/me/events/'+quote(master,safe='')+'?$select=iCalUId',token).get('iCalUId')
                    uid=masters[master];occurrence=instant(row['originalStart'])
                elif row.get('type')!='singleInstance':raise ImportError('unexpected_series_master')
                events.append({'uid':uid,'occurrence':occurrence,'title':row.get('subject') or '予定','start':graph_time(row['start']),'end':graph_time(row['end']),'all_day':row.get('isAllDay',False),'cancelled':row.get('isCancelled',False)})
            url=page.get('@odata.nextLink')
            if url is not None and not isinstance(url,str):raise ImportError('graph_page')
        return {'version':1,'complete':True,'source':'graph','scope':self.scope,'from':first,'to':last,'events':events}
