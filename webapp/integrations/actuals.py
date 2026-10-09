"""Core projection of Task and normalized external actuals. No provider imports."""
from .contracts import ActualRecord


def task_actuals(snapshot):
    records=[]
    for entity in snapshot.entities:
        if entity.entity_type!='task':continue
        fm=entity.frontmatter;first=fm.get('work_started_at') or fm.get('started_at');last=fm.get('work_ended_at') if fm.get('work_started_at') else fm.get('completed_at')
        if not first:continue
        # Historical Task fields carry no measurement-origin flag. Do not invent one.
        try:
            records.append(ActualRecord.create(reference_id='task:'+entity.entity_id,source_kind='task',source_id=entity.entity_id,source_provider='core',title=fm.get('title',entity.entity_id),start=first,end=last or None,origin='recorded',completed=fm.get('status')=='done',provisional=not bool(last)))
        except (ValueError,TypeError):continue
    return records


def external_actual(*, source_id, provider, title, actual, completed):
    return ActualRecord.create(reference_id='external:'+source_id,source_kind='external_event',source_id=source_id,source_provider=provider,title=title,start=actual['start'],end=actual.get('end'),origin=actual['origin'],completed=completed,provisional=actual.get('end') is None)
