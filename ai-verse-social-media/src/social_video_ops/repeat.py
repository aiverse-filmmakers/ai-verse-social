"""Local sampled-footage guard. Similarity creates review evidence, never identity proof."""
from __future__ import annotations
import json
import math
import os
import statistics
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .media import probe
from .util import UserError, canonical, digest, file_hash, now, write_json

ALGORITHM = 'dct32-low8-ac63-v1'
COS = [[math.cos((2*x+1)*u*math.pi/64) for x in range(32)] for u in range(8)]


def phash(pixels: bytes) -> str:
    if len(pixels) != 1024: raise UserError('Frame extraction returned invalid pixels.')
    horizontal = [[sum(pixels[y*32+x]*COS[u][x] for x in range(32)) for u in range(8)] for y in range(32)]
    values = [sum(horizontal[y][u]*COS[v][y] for y in range(32)) for v in range(8) for u in range(8)]
    median = statistics.median(values[1:])
    # DC is excluded and bit 63 is fixed; distances are measured on the 63 AC bits.
    return f'{sum((value>median)<<i for i,value in enumerate(values[1:])):016x}'


def distance(a: str, b: str) -> int:
    return (int(a,16)^int(b,16)).bit_count()


class RepeatGuard:
    def __init__(self, engine):
        self.engine, self.store, self.workspace = engine, engine.store, engine.workspace

    @property
    def options(self): return self.workspace.config['repeat_guard']

    def cached_data(self, row, asset):
        if not row or row['status']!='ready' or row['source_hash']!=asset['hash'] or row['algorithm']!=ALGORITHM: return None
        try:
            data=json.loads(row['data'])
            if not isinstance(data,dict) or data.get('source_hash')!=asset['hash'] or data.get('algorithm')!=ALGORITHM: return None
            if type(data.get('duration')) not in (int,float) or not math.isfinite(data['duration']) or data['duration']<=0: return None
            source=Path(asset['path']).stat()
            if data.get('source_stat')!=[source.st_size,source.st_mtime_ns]: return None
            frames=data.get('frames')
            if not isinstance(frames,list) or len(frames)!=3: return None
            for index,frame in enumerate(frames):
                expected=f"media/repeat/{asset['hash']}/{ALGORITHM}/{index}.png"
                if not isinstance(frame,dict) or frame.get('path')!=expected or type(frame.get('informative')) is not bool: return None
                value=frame.get('hash')
                if not isinstance(value,str) or len(value)!=16 or any(c not in '0123456789abcdef' for c in value): return None
                path=self.workspace.path/expected
                if path.is_symlink() or not path.is_file() or path.stat().st_size>262144 or file_hash(path)!=frame.get('checksum'): return None
            return data
        except (OSError,ValueError,TypeError,KeyError): return None

    def fingerprint(self, asset_id: str, budget=60) -> dict:
        asset = self.store.require_asset(asset_id)
        source_stat=Path(asset['path']).stat()
        signature=[source_stat.st_size,source_stat.st_mtime_ns]
        cached = self.store.one('SELECT * FROM repeat_fingerprints WHERE asset_id=?',(asset_id,))
        data=self.cached_data(cached,asset)
        if data and data.get('source_stat')==signature: return data
        if file_hash(Path(asset['path'])) != asset['hash']:
            self.store.db.execute("UPDATE repeat_fingerprints SET status='pending' WHERE asset_id=?",(asset_id,))
            raise UserError('Repeat-check source changed; preserve it and ingest its new revision.')
        start = time.monotonic()
        try:
            metadata=probe(Path(asset['path']),decode=False,timeout=min(60,budget))
            directory=self.workspace.path/'media/repeat'/asset['hash']/ALGORITHM
            directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            frames=[]
            for index,fraction in enumerate((.2,.5,.8)):
                remaining=min(20,budget-(time.monotonic()-start))
                if remaining<=0: raise UserError('Repeat extraction budget reached; continue in a separate batch.')
                target=directory/f'{index}.png'
                temporary=directory/f'.{index}-{uuid.uuid4().hex}.png'
                result=subprocess.run(['ffmpeg','-nostdin','-v','error','-ss',str(metadata['duration']*fraction),'-i',asset['path'],
                    '-map','0:v:0','-frames:v','1','-vf','scale=32:32:flags=area,format=gray','-f','rawvideo','pipe:1',
                    '-map','0:v:0','-frames:v','1','-vf','scale=192:108:force_original_aspect_ratio=decrease,pad=192:108:(ow-iw)/2:(oh-ih)/2','-update','1','-y',str(temporary)],capture_output=True,timeout=remaining)
                if result.returncode or len(result.stdout)!=1024: raise UserError('Could not extract a repeat-check frame.')
                pixels=result.stdout
                os.replace(temporary,target)
                frames.append({'hash':phash(pixels),'informative':statistics.pstdev(pixels)>=10,
                    'path':str(target.relative_to(self.workspace.path)),'checksum':file_hash(target)})
            data={'duration':metadata['duration'],'frames':frames,'algorithm':ALGORITHM,'source_hash':asset['hash'],'source_stat':signature}
            if file_hash(Path(asset['path']))!=asset['hash']: raise UserError('Source changed while extracting repeat frames.')
            self.store.db.execute("INSERT INTO repeat_fingerprints VALUES(?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET source_hash=excluded.source_hash,algorithm=excluded.algorithm,data=excluded.data,status=excluded.status,error=excluded.error,updated=excluded.updated",
                (asset_id,asset['hash'],ALGORITHM,canonical(data),'ready',None,now()))
            return data
        except (OSError,ValueError,UserError,subprocess.SubprocessError) as exc:
            if 'temporary' in locals(): temporary.unlink(missing_ok=True)
            self.store.db.execute("INSERT INTO repeat_fingerprints VALUES(?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET status='pending',error=excluded.error,updated=excluded.updated",
                (asset_id,asset['hash'],ALGORITHM,'{}','pending','Repeat extraction unavailable; check source and FFmpeg.',now()))
            raise UserError('Repeat extraction unavailable; check source and FFmpeg. Nothing was published.') from exc

    def history(self, candidate=None):
        profiles=self.workspace.config['profiles']
        if not profiles: return []
        cutoff=(datetime.now(timezone.utc)-timedelta(days=self.options['window_days'])).isoformat()
        marks=','.join('?' for _ in profiles)
        rows=self.store.rows(f"""SELECT DISTINCT a.*,m.work_id FROM assets a JOIN work_members m ON m.asset_id=a.id
          JOIN jobs j ON j.asset_id=a.id JOIN destinations d ON d.id=j.destination_id
          WHERE d.profile_id IN ({marks}) AND ((j.state='verified' AND j.updated>=?) OR
            j.state IN ('prepared','repeat_hold','submitting','scheduled','verification_pending','outcome_unknown','cancel_pending','provider_draft'))
          ORDER BY a.created,a.id""",(*profiles,cutoff))
        if candidate:
            work=self.store.work_id(candidate)
            rows=[row for row in rows if row['work_id']!=work]
        return rows

    def index(self, limit=3):
        if type(limit)!=int or not 1<=limit<=20: raise UserError('Repeat indexing limit must be between 1 and 20.')
        start=time.monotonic();results=[]
        cursor=self.store.meta('repeat_index_cursor','')
        rows=self.history()
        profiles=self.workspace.config['profiles']
        if profiles:
            marks=','.join('?' for _ in profiles)
            recent=self.store.rows(f"SELECT m.work_id,MAX(j.updated) AS published FROM jobs j JOIN work_members m ON m.asset_id=j.asset_id JOIN destinations d ON d.id=j.destination_id WHERE j.state='verified' AND d.profile_id IN ({marks}) GROUP BY m.work_id ORDER BY published DESC LIMIT 9",profiles)
            known={row['id'] for row in rows}
            for work in recent:
                asset=self.store.one(f"SELECT a.* FROM assets a JOIN work_members m ON m.asset_id=a.id JOIN jobs j ON j.asset_id=a.id JOIN destinations d ON d.id=j.destination_id WHERE m.work_id=? AND j.state='verified' AND d.profile_id IN ({marks}) ORDER BY j.updated DESC LIMIT 1",(work['work_id'],*profiles))
                if asset['id'] not in known: rows.append(asset);known.add(asset['id'])
        rows.sort(key=lambda r:(r['id']<=cursor,r['id']))
        for asset in rows:
            cached=self.store.one("SELECT * FROM repeat_fingerprints WHERE asset_id=?",(asset['id'],))
            if self.cached_data(cached,asset): continue
            if len(results)>=limit or time.monotonic()-start>=self.options['batch_seconds']: break
            self.store.set_meta('repeat_index_cursor',asset['id'])
            try: self.fingerprint(asset['id'],budget=min(60,self.options['batch_seconds']-(time.monotonic()-start)));results.append({'asset_id':asset['id'],'state':'ready'})
            except (UserError,OSError): results.append({'asset_id':asset['id'],'state':'check_pending'})
        return {'results':results}

    def inspect_cached(self, asset_id, destination_ids=None):
        """CPU-only check: can run inside an existing IMMEDIATE transaction."""
        asset=self.store.require_asset(asset_id);work=self.store.work_id(asset_id)
        destinations=destination_ids or []
        claimed=[{'destination_id':d,'job':self.store.work_job(asset_id,d)} for d in destinations]
        covered=[x for x in claimed if x['job'] and x['job']['state']=='verified']
        reserved=[x for x in claimed if x['job'] and x['job']['asset_id']!=asset_id and x['job']['state']!='verified']
        result={'asset_id':asset_id,'work_id':work,'status':'clear','matches':[], 'covered':covered,'reserved':reserved}
        if not self.options['enabled']:
            if not self.store.meta('repeat_policy_note'):
                result['status']='check_pending';result['reason']='record_visual_check_choice'
            return result
        policy=digest(self.options)
        own=self.store.one("SELECT * FROM repeat_fingerprints WHERE asset_id=? AND status='ready'",(asset_id,))
        candidate=self.cached_data(own,asset)
        if not candidate:
            result['status']='check_pending';return result
        try:
            signature=Path(asset['path']).stat()
            if candidate.get('source_stat')!=[signature.st_size,signature.st_mtime_ns]:
                result['status']='check_pending';return result
        except OSError:
            result['status']='check_pending';return result
        frames=candidate['frames']
        useful=[f for f in frames if f['informative']]
        if len(useful)<2 or all(distance(useful[0]['hash'],f['hash'])<=2 for f in useful):
            # Low-information material needs explicit review, even without a matched work.
            reviewed=self.store.one("SELECT id FROM repeat_decisions WHERE asset_id=? AND source_hash=? AND matched_work='' AND decision='distinct' AND policy_hash=?",(asset_id,asset['hash'],policy))
            if not reviewed: result['status']='needs_review';result['reason']='uninformative_frames'
        for previous in self.history(asset_id):
            row=self.store.one("SELECT * FROM repeat_fingerprints WHERE asset_id=? AND status='ready'",(previous['id'],))
            data=self.cached_data(row,previous)
            if not data:
                result['status']='check_pending';return result
            if abs(candidate['duration']-data['duration'])/max(candidate['duration'],data['duration'])>self.options['duration_tolerance']: continue
            differences=[distance(a['hash'],b['hash']) if a['informative'] and b['informative'] else None for a,b in zip(frames,data['frames'])]
            if sum(v is not None and v<=self.options['hamming_bits'] for v in differences)<2: continue
            distinct=self.store.one("SELECT id FROM repeat_decisions WHERE asset_id=? AND source_hash=? AND matched_work=? AND matched_hash=? AND decision='distinct' AND policy_hash=?",
                (asset_id,asset['hash'],previous['work_id'],previous['hash'],policy))
            if not distinct:
                result['status']='needs_review';result['matches'].append({'work_id':previous['work_id'],'asset_id':previous['id'],'source_hash':previous['hash'],'distances':differences})
        return result

    def check(self, asset_id, destination_ids=None):
        if self.options['enabled']:
            try:
                self.fingerprint(asset_id)
                self.index(self.options['index_batch'])
            except (UserError,OSError): pass
        with self.store.transaction():
            result=self.inspect_cached(asset_id,destination_ids)
            if result['status']!='clear':
                self.store.db.execute("INSERT INTO repeat_holds VALUES(?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET status=excluded.status,data=excluded.data,updated=excluded.updated",
                    (asset_id,result['status'],canonical(result),now()))
            else:
                self.store.db.execute('DELETE FROM repeat_holds WHERE asset_id=?',(asset_id,))
                self.store.db.execute("UPDATE jobs SET state='prepared',error=NULL,updated=? WHERE asset_id=? AND state='repeat_hold' AND submitted IS NULL AND provider_id IS NULL",(now(),asset_id))
        return result

    def review(self, asset_id, decision, matched_work, note):
        asset=self.store.require_asset(asset_id)
        if decision not in {'same','distinct'} or not isinstance(note,str) or not note.strip(): raise UserError('Record an explicit same/distinct review and evidence note.')
        work=self.store.work_id(asset_id)
        matches=self.store.rows('SELECT a.* FROM assets a JOIN work_members m ON m.asset_id=a.id WHERE m.work_id=?',(matched_work,)) if matched_work else []
        if matched_work and not matches: raise UserError('Unknown matched work.')
        if decision=='same' and (not matched_work or matched_work==work): raise UserError('Choose a different existing matched work.')
        with self.store.transaction():
            if decision=='same':
                conflict=self.store.one("SELECT j.id FROM jobs j JOIN work_members m ON m.asset_id=j.asset_id WHERE m.work_id IN (?,?) AND (COALESCE(j.lease_until,0)>? OR j.state NOT IN ('verified','cancelled','failed')) LIMIT 1",(work,matched_work,time.time()))
                if conflict: raise UserError('Reconcile/cancel active or uncertain jobs before linking works.')
                for job in self.store.rows("SELECT j.* FROM jobs j JOIN work_members m ON m.asset_id=j.asset_id WHERE m.work_id IN (?,?) AND j.state='failed' AND j.submitted IS NOT NULL",(work,matched_work)):
                    if self.store.meta('nonpublication:'+job['id'])!='1':
                        raise UserError('Reconcile failed submission evidence before linking works.')
                affected=[r['asset_id'] for r in self.store.rows('SELECT asset_id FROM work_members WHERE work_id IN (?,?)',(work,matched_work))]
                for member in affected:
                    self.store.db.execute('DELETE FROM repeat_decisions WHERE asset_id=?',(member,))
                self.store.db.execute('DELETE FROM repeat_decisions WHERE matched_work IN (?,?)',(work,matched_work))
                self.store.db.execute('DELETE FROM work_destination_claims WHERE work_id=?',(work,))
                self.store.db.execute('UPDATE work_members SET work_id=? WHERE work_id=?',(matched_work,work))
                for job in self.store.rows("SELECT j.* FROM jobs j JOIN work_members m ON m.asset_id=j.asset_id WHERE m.work_id=? AND j.state='verified' ORDER BY j.updated,j.id",(matched_work,)):
                    self.store.db.execute('INSERT OR IGNORE INTO work_destination_claims VALUES(?,?,?,?)',(matched_work,job['destination_id'],job['id'],now()))
            for previous in matches or [{'hash':''}]:
                self.store.db.execute('INSERT INTO repeat_decisions VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),asset_id,asset['hash'],matched_work,previous['hash'],decision,digest(self.options),note,now()))
            self.store.db.execute('DELETE FROM repeat_holds WHERE asset_id=?',(asset_id,))
            self.store.event('repeat-review',asset_id,{'decision':decision,'matched_work':matched_work,'note':note})
        self.queue_sources()
        return {'asset_id':asset_id,'work_id':self.store.work_id(asset_id),'decision':decision,'next':'repeat-check; existing account receipts remain authoritative'}

    def readiness(self):
        """Inspect cache readiness without decoding or remote access."""
        if not self.options['enabled']:
            return {'status':'disabled','recorded_choice':bool(self.store.meta('repeat_policy_note'))}
        missing=[]
        for asset in self.history():
            data=self.cached_data(self.store.one('SELECT * FROM repeat_fingerprints WHERE asset_id=?',(asset['id'],)),asset)
            if not data: missing.append(asset['id'])
        return {'status':'check_pending' if missing else 'ready','pending_assets':missing,'next':'repeat-index --limit 3' if missing else ''}

    def set_policy(self, enabled, note):
        if type(enabled) is not bool or not isinstance(note,str) or not note.strip():
            raise UserError('Record the customer choice and reason before changing visual repeat checks.')
        config=self.workspace.config;config['repeat_guard']['enabled']=enabled
        self.workspace.save(config)
        with self.store.transaction():
            self.store.set_meta('repeat_policy_note',note)
            self.store.event('repeat-policy','workspace',{'enabled':enabled,'note':note,'exact_byte_protection':True})
        return {'enabled':enabled,'exact_byte_protection':True,'next':'audit; repeat-check held candidates'}

    def panel(self, asset_id=None):
        """Render cached evidence; extraction uses one bounded indexing batch."""
        import base64
        from html import escape
        from .util import atomic_write
        profiles=self.workspace.config['profiles']
        if not profiles: raise UserError('Choose customer profiles before generating the panel.')
        check=self.check(asset_id) if asset_id else None
        if not asset_id: self.index(self.options['index_batch'])
        marks=','.join('?' for _ in profiles)
        rows=self.store.rows(f"""SELECT m.work_id,max(j.updated) AS published FROM jobs j JOIN work_members m ON m.asset_id=j.asset_id
            JOIN destinations d ON d.id=j.destination_id WHERE j.state='verified' AND d.profile_id IN ({marks})
            GROUP BY m.work_id ORDER BY published DESC,m.work_id LIMIT 9""",profiles)
        tiles=[];missing=[]
        def cached(asset):
            row=self.store.one('SELECT * FROM repeat_fingerprints WHERE asset_id=?',(asset['id'],))
            data=self.cached_data(row,asset)
            if not data: missing.append(asset['id'])
            return data
        for row in rows:
            asset=self.store.one("SELECT a.* FROM assets a JOIN work_members m ON m.asset_id=a.id JOIN jobs j ON j.asset_id=a.id JOIN destinations d ON d.id=j.destination_id WHERE m.work_id=? AND j.state='verified' AND d.profile_id IN ("+marks+") ORDER BY j.updated DESC LIMIT 1",(row['work_id'],*profiles))
            data=cached(asset);tiles.append({**row,'asset':asset,'frame':data['frames'][1] if data else None})
        matches=check['matches'] if check else []
        # Deduplicate match evidence by work, retaining all three samples.
        evidence={}
        for match in matches:
            if match['work_id'] in evidence: continue
            data=cached(self.store.require_asset(match['asset_id']))
            if data: evidence[match['work_id']]=data
        width=840 if asset_id else 630
        height=480+150*len(evidence)
        parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#111827"/>']
        def image(frame,x,y,label):
            if frame:
                raw=(self.workspace.path/frame['path']).read_bytes()
                encoded=base64.b64encode(raw).decode()
                parts.append(f'<image x="{x}" y="{y}" width="192" height="108" href="data:image/png;base64,{encoded}"/>')
            else:
                parts.append(f'<rect x="{x}" y="{y}" width="192" height="108" fill="#374151"/>')
                parts.append(f'<text x="{x+8}" y="{y+55}" fill="white" font-size="12">Index pending</text>')
            parts.append(f'<text x="{x}" y="{y+127}" fill="white" font-size="12" font-family="sans-serif">{escape(label[:55])}</text>')
        for index,tile in enumerate(tiles):
            image(tile['frame'],10+(index%3)*210,15+(index//3)*155,f"{index+1}. {tile['published'][:10]} {tile['asset']['title'][:24]}")
        if asset_id:
            data=cached(self.store.require_asset(asset_id))
            for index in range(3): image(data['frames'][index] if data else None,640,15+index*155,f'Candidate {(.2,.5,.8)[index]:.0%}')
        for index,(work,data) in enumerate(evidence.items()):
            for sample,frame in enumerate(data['frames']):
                image(frame,10+sample*210,490+index*150,f'Match {work[:12]} {(.2,.5,.8)[sample]:.0%}')
        parts.append('</svg>')
        directory=self.workspace.path/'media/repeat/panels';directory.mkdir(parents=True,exist_ok=True)
        path=directory/('comparison-'+asset_id+'.svg' if asset_id else 'latest-nine.svg')
        atomic_write(path,'\n'.join(parts))
        legend={'tiles':[{'tile':i+1,'work_id':t['work_id'],'published':t['published'],'title':t['asset']['title']} for i,t in enumerate(tiles)],'matched_evidence':list(evidence),'pending_assets':sorted(set(missing))}
        write_json(path.with_suffix('.json'),legend)
        return {'panel':str(path),'legend':str(path.with_suffix('.json')),'unique_works':len(tiles),'matches':matches,'status':'check_pending' if missing else 'ready','pending_assets':legend['pending_assets']}

    def queue_sources(self):
        ready=self.workspace.config['drive']['folders'].get('ready')
        if not ready: return
        for source in self.store.rows("SELECT s.*,a.drive_id FROM asset_sources s JOIN assets a ON a.id=s.asset_id WHERE s.provider='gdrive' AND s.parent=?",(ready,)):
            work=self.store.work_id(source['asset_id'])
            extra=(source['source_id']!=source['drive_id'] or work!=source['asset_id'])
            if not extra: continue
            if source['source_id']==source['drive_id'] and self.store.one("SELECT 1 FROM jobs WHERE asset_id=? AND state='verified' LIMIT 1",(source['asset_id'],)): continue
            published=self.store.one("SELECT 1 FROM jobs j JOIN work_members m ON m.asset_id=j.asset_id WHERE m.work_id=? AND j.state='verified' LIMIT 1",(work,))
            if not published: continue
            self.store.db.execute("INSERT OR IGNORE INTO repeat_source_actions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (source['source_id'],source['asset_id'],work,ready,source['version'],None,None,'pending',None,now()))

    def sync_sources(self, restore=None):
        from .drive import Drive
        config=self.workspace.config;folders=config['drive']['folders']
        if not config['drive']['enabled']: return {'state':'drive_not_connected','local_blocking_preserved':True}
        if not folders.get('repeat_review'): return {'state':'needs_repeat_review_folder','local_blocking_preserved':True}
        if self.store.meta('restore_reconciliation_required')=='1': raise UserError('Restored workspace is quarantined; reconcile before Drive filing.')
        self.queue_sources();drive=Drive(config['drive']);results=[]
        rows=self.store.rows('SELECT * FROM repeat_source_actions WHERE source_id=?',(restore,)) if restore else self.store.rows("SELECT * FROM repeat_source_actions WHERE state IN ('pending','move_pending','restore_pending') ORDER BY updated LIMIT ?",(self.options['index_batch'],))
        if restore and not rows: raise UserError('Unknown repeat source; no folder changed.')
        for row in rows:
            restoring=bool(restore) or row['state']=='restore_pending'
            asset=self.store.require_asset(row['asset_id']);work=self.store.work_id(asset['id'])
            try:
                if not restoring:
                    # Request-only/library policy is preserved. Outstanding authorised
                    # obligations or provider outcomes can still need this material.
                    waiting=self.store.one("""SELECT 1 FROM obligations o JOIN work_members m ON m.asset_id=o.asset_id JOIN destinations d ON d.id=o.destination_id
                        WHERE m.work_id=? AND d.enabled=1 AND NOT EXISTS
                        (SELECT 1 FROM work_destination_claims c JOIN jobs j ON j.id=c.job_id
                         WHERE c.work_id=m.work_id AND c.destination_id=o.destination_id AND j.state='verified') LIMIT 1""",(work,))
                    if waiting:
                        results.append({'source_id':row['source_id'],'state':'waiting_for_authorized_accounts'});continue
                current=self.workspace.config
                if not current['drive']['enabled'] or current['drive']['folders'].get('repeat_review')!=folders['repeat_review'] or current['drive']['folders'].get('ready')!=row['original_parent']:
                    raise UserError('Drive folder scope changed; review before filing.')
                target=row['original_parent'] if restoring else folders['repeat_review']
                observed=drive.metadata(row['source_id'])
                if observed.get('trashed'): raise UserError('Repeat source is unavailable.')
                checksum=file_hash(Path(asset['path']),'md5')
                if observed.get('md5Checksum')!=checksum: raise UserError('Repeat source checksum changed; do not move it.')
                parents=observed.get('parents',[])
                expected=folders['repeat_review'] if restoring else row['original_parent']
                if target in parents:
                    # A previously uncertain move is reconciled by checksum/parent,
                    # never resubmitted as a post or assumed from cached metadata.
                    moved=observed
                else:
                    if expected not in parents: raise UserError('Repeat source moved outside approved folders.')
                    if row['version'] and str(observed.get('version'))!=str(row['version']):
                        raise UserError('Repeat source revision changed before filing.')
                    with self.store.transaction():
                        self.store.db.execute("UPDATE repeat_source_actions SET state=?,checksum=?,target_parent=?,updated=? WHERE source_id=?",
                            ('restore_pending' if restoring else 'move_pending',checksum,target,now(),row['source_id']))
                    drive.move(row['source_id'],expected,target)
                    moved=drive.metadata(row['source_id'])
                if target not in moved.get('parents',[]) or moved.get('md5Checksum')!=checksum: raise UserError('Repeat filing was not confirmed; retain pending action.')
                with self.store.transaction():
                    self.store.db.execute('UPDATE repeat_source_actions SET state=?,error=NULL,version=?,updated=? WHERE source_id=?',
                        ('restored' if restoring else 'filed',moved.get('version'),now(),row['source_id']))
                    self.store.db.execute("UPDATE asset_sources SET parent=?,version=? WHERE provider='gdrive' AND source_id=?",(target,moved.get('version'),row['source_id']))
                    self.store.event('repeat-source-restored' if restoring else 'repeat-source-filed',asset['id'],{'source_id':row['source_id'],'target':target})
                results.append({'source_id':row['source_id'],'state':'restored' if restoring else 'filed','receipts_preserved':True})
            except (UserError,OSError) as exc:
                self.store.db.execute('UPDATE repeat_source_actions SET error=?,updated=? WHERE source_id=?',(str(exc),now(),row['source_id']))
                results.append({'source_id':row['source_id'],'state':'filing_pending','error':str(exc)})
        return {'results':results}
