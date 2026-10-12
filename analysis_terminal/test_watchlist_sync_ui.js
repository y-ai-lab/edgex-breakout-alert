// Shipped watchlist client; synthetic capability and mocked HTTP only.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
const KEY='synthetic-watchlist-key-00000001',OTHER='synthetic-watchlist-key-00000002',storage=new Map(),nodes={},timers=new Map();let id=0,calls=[],quick=0,rows=0,persistFail=false;
for(const k of ['watchSyncStatus','watchSaveStatus','watchSyncKey','watchSyncUseLocal','watchSyncUseRemote','watchSyncRefresh','watchConflictDetails'])nodes[k]={value:'',textContent:'',hidden:false,disabled:false,attrs:{},setAttribute(k,v){this.attrs[k]=v}};nodes.watchSyncKey.value=KEY;
const snapshot=(tickers=['BTCUSDC'],revision=10,exists=true)=>({exists,revision,tickers});
const ctx={Set,Number,Array,JSON,Object,Promise,AbortController,Uint32Array,crypto:require('crypto').webcrypto,watch:new Set(['BTCUSDC']),watchSyncKey:KEY,browserStorageRead:k=>storage.get(k)||null,browserStorageWrite(k,v){if(persistFail)return false;storage.set(k,v);return true},renderQuickAccess(){quick++},renderRows(){rows++},document:{getElementById:k=>nodes[k]},setTimeout(fn,ms){const key=++id;timers.set(key,{fn,ms});return key},clearTimeout:k=>timers.delete(k),
 fetch:async(url,opt)=>{calls.push({url,opt});const result=await ctx.reply(url,opt);return result&&result.httpStatus?{ok:false,status:result.httpStatus,json:async()=>({detail:'PRIVATE_ERROR'})}:{ok:true,status:200,json:async()=>result}},reply:async(url,opt)=>opt.method==='POST'?{ok:true,...snapshot(JSON.parse(opt.body).tickers,11)}:snapshot()};
vm.createContext(ctx);vm.runInContext(script.slice(script.indexOf('var watchEditRevision='),script.indexOf('function tab(name)')),ctx);
const tick=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate)},defer=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}},read=(replace=false,key=KEY)=>ctx.loadWatchFromServer(key,replace),posts=()=>calls.filter(x=>x.opt.method==='POST'),name=process.argv[3];
(async()=>{
 if(name==='same_list_saved_status'){
  await read();assert.equal(posts().length,0);assert(nodes.watchSyncStatus.textContent.includes('確認済み'));assert.equal(ctx.watchSyncDirty,false);assert.equal(ctx.watchRemoteSnapshot.revision,10);assert.equal(storage.get('edgexWatchSyncDirty'),'false');
 }else if(name==='local_edit_during_startup_kept'){
  const d=defer();ctx.reply=()=>d.promise;const pending=read();ctx.watch.add('SOLUSDC');ctx.saveWatch();assert.equal(posts().length,0);d.resolve(snapshot(['ETHUSDC']));await pending;assert.deepEqual([...ctx.watch],['BTCUSDC','SOLUSDC']);assert(ctx.watchSyncConflict);assert.equal(posts().length,0);assert.equal(storage.get('edgexWatchSyncDirty'),'true');
 }else if(name==='initial_conflict_not_overwritten'){
  ctx.reply=async()=>snapshot(['ETHUSDC']);await read();assert.deepEqual([...ctx.watch],['BTCUSDC']);assert(ctx.watchSyncConflict);assert(!nodes.watchSyncUseLocal.hidden);assert(!nodes.watchSyncUseRemote.hidden);assert(nodes.watchConflictDetails.textContent.includes('ETHUSDC'));assert.equal(posts().length,0);
 }else if(name==='existing_empty_is_conflict'){
  ctx.reply=async()=>snapshot([]);await read();assert(ctx.watchSyncConflict);assert.deepEqual([...ctx.watch],['BTCUSDC']);assert.equal(posts().length,0);
 }else if(name==='nonexistent_seed_uses_zero_revision'){
  ctx.reply=async(u,o)=>o.method==='POST'?{ok:true,...snapshot(JSON.parse(o.body).tickers,1)}:snapshot([],0,false);await read();await tick();assert.equal(posts().length,1);assert.equal(JSON.parse(posts()[0].opt.body).expected_revision,0);assert.deepEqual([...ctx.watch],['BTCUSDC']);assert.equal(ctx.watchSyncDirty,false);
 }else if(name==='remote_bootstrap_and_explicit_empty'){
  ctx.watch.clear();ctx.reply=async()=>snapshot(['ETHUSDC']);await read();assert.deepEqual([...ctx.watch],['ETHUSDC']);assert(rows>0);ctx.reply=async()=>snapshot([],11);await read(true);assert.equal(ctx.watch.size,0);assert.equal(ctx.watchSyncKey,KEY);assert.equal(storage.get('edgexWatch'),'[]');
 }else if(name==='dirty_empty_not_recreated'){
  ctx.watch.clear();ctx.watchSyncDirty=true;ctx.reply=async()=>snapshot(['ETHUSDC']);await read();assert.equal(ctx.watch.size,0);assert(ctx.watchSyncConflict);assert.equal(posts().length,0);
 }else if(name==='missing_import_keeps_local_key'){
  storage.set('edgexWatchSyncKey',KEY);ctx.reply=async()=>snapshot([],0,false);await read(true,OTHER);assert.equal(ctx.watchSyncKey,KEY);assert.equal(storage.get('edgexWatchSyncKey'),KEY);assert.deepEqual([...ctx.watch],['BTCUSDC']);assert(nodes.watchSyncStatus.textContent.includes('見つかりません'));
 }else if(name==='edited_during_new_key_import'){
  const d=defer();ctx.reply=()=>d.promise;const pending=read(true,OTHER);ctx.watch.add('SOLUSDC');ctx.saveWatch();d.resolve(snapshot(['ETHUSDC']));await pending;assert.equal(ctx.watchSyncKey,KEY);assert.deepEqual([...ctx.watch],['BTCUSDC','SOLUSDC']);assert(nodes.watchSyncStatus.textContent.includes('保持'));
 }else if(name==='serialized_latest_changes'){
  await read();const d=defer();let n=0;ctx.reply=async(u,o)=>{n++;return n===1?d.promise:{ok:true,...snapshot(JSON.parse(o.body).tickers,12)}};ctx.watch.add('ETHUSDC');ctx.saveWatch();ctx.watch.add('SOLUSDC');ctx.watch.delete('BTCUSDC');ctx.saveWatch();assert.equal(posts().length,1);d.resolve({ok:true,...snapshot(['BTCUSDC','ETHUSDC'],11)});await tick();assert.equal(posts().length,2);const latest=JSON.parse(posts()[1].opt.body);assert.equal(latest.expected_revision,11);assert.deepEqual(latest.tickers,['ETHUSDC','SOLUSDC']);assert.equal(ctx.watchSyncDirty,false);
 }else if(name==='conflict_reply_preserves_local'){
  await read();ctx.reply=async()=>({httpStatus:409});ctx.watch.add('ETHUSDC');ctx.saveWatch();await tick();assert(ctx.watchSyncUncertain);assert.deepEqual([...ctx.watch],['BTCUSDC','ETHUSDC']);assert(nodes.watchSyncStatus.textContent.includes('別の保存'));ctx.watch.add('SOLUSDC');ctx.saveWatch();await tick();assert.equal(posts().length,1);
 }else if(name==='write_timeout_late_reply_no_false_success'){
  await read();const d=defer();ctx.reply=()=>d.promise;ctx.watch.add('ETHUSDC');ctx.saveWatch();await tick();timers.values().next().value.fn();await tick();assert(ctx.watchSyncUncertain);assert(nodes.watchSyncStatus.textContent.includes('保存を確認できません'));d.resolve({ok:true,...snapshot(['BTCUSDC','ETHUSDC'],11)});await tick();assert(ctx.watchSyncUncertain);assert(!nodes.watchSyncStatus.textContent.includes('確認済み'));
 }else if(name==='read_timeout_then_retry'){
  const d=defer();ctx.reply=()=>d.promise;const pending=read();timers.values().next().value.fn();await pending;assert(ctx.watchSyncUncertain);assert.equal(ctx.watch.size,1);ctx.reply=async()=>snapshot();await read();d.resolve(snapshot(['ETHUSDC']));await tick();assert.equal(ctx.watchSyncUncertain,false);assert.deepEqual([...ctx.watch],['BTCUSDC']);assert.equal(timers.size,0);
 }else if(name==='invalid_response_not_empty_or_applied'){
  for(const bad of [{tickers:[]},{...snapshot(),revision:-1},{...snapshot(),revision:9007199254740992},snapshot([null]),snapshot(['BTCUSDC','BTCUSDC']),snapshot([' btc ']),snapshot(['BTCUSDC'],0,false)]){ctx.reply=async()=>bad;await read();assert(ctx.watchSyncUncertain);assert.deepEqual([...ctx.watch],['BTCUSDC']);assert.equal(posts().length,0)}
 }else if(name==='malformed_save_reply'){
  await read();ctx.reply=async()=>({ok:true,...snapshot(['UNKNOWNUSDC'],11)});ctx.watch.add('ETHUSDC');ctx.saveWatch();await tick();assert(ctx.watchSyncUncertain);assert(ctx.watchSyncDirty);assert.deepEqual([...ctx.watch],['BTCUSDC','ETHUSDC']);
 }else if(name==='manual_local_choice_still_checks_revision'){
  ctx.reply=async(u,o)=>o.method==='POST'?{ok:true,...snapshot(JSON.parse(o.body).tickers,11)}:snapshot(['ETHUSDC']);await read();await ctx.saveWatchToServer(true);assert.equal(JSON.parse(posts()[0].opt.body).expected_revision,10);assert.deepEqual([...ctx.watch],['BTCUSDC']);assert.equal(ctx.watchSyncDirty,false);
 }else if(name==='manual_remote_choice_refreshes_actual_data'){
  ctx.reply=async()=>snapshot(['ETHUSDC']);await read();ctx.reply=async()=>snapshot(['SOLUSDC'],11);await read(true);assert.deepEqual([...ctx.watch],['SOLUSDC']);assert(!ctx.watchSyncConflict);assert.equal(posts().length,0);
 }else if(name==='storage_failure_not_claimed_persisted'){
  await read();persistFail=true;ctx.watch.add('ETHUSDC');ctx.saveWatch();await tick();assert.equal(ctx.watchLocalSaved,false);assert(nodes.watchSaveStatus.textContent.includes('この画面内のみ'));assert(!nodes.watchSaveStatus.textContent.includes('端末に保存済み'));
 }else if(name==='old_key_post_cannot_restore_new_key'){
  await read();const d=defer();ctx.reply=async(u,o)=>o.method==='POST'?d.promise:snapshot(['SOLUSDC'],20);ctx.watch.add('ETHUSDC');ctx.saveWatch();await read(true,OTHER);assert.equal(ctx.watchSyncKey,OTHER);d.resolve({ok:true,...snapshot(['BTCUSDC','ETHUSDC'],11)});await tick();assert.equal(ctx.watchSyncKey,OTHER);assert.deepEqual([...ctx.watch],['SOLUSDC']);assert.equal(ctx.watchRemoteSnapshot.revision,20);
 }else if(name==='key_in_header_and_no_error_echo'){
  ctx.reply=async()=>{throw Error('PRIVATE_ERROR')};await read();assert.equal(calls[0].url,'/api/watchlist');assert.equal(calls[0].opt.headers['X-Watch-Sync-Key'],KEY);assert(!nodes.watchSyncStatus.textContent.includes(KEY));assert(!nodes.watchSyncStatus.textContent.includes('PRIVATE_ERROR'));
 }else if(name==='typed_key_does_not_redirect_pending_save'){
  ctx.reply=async()=>snapshot(['ETHUSDC']);await read();nodes.watchSyncKey.value=OTHER;ctx.renderWatchSyncControls();assert(nodes.watchSyncUseLocal.disabled);assert(nodes.watchSyncUseRemote.disabled);assert.equal(ctx.watchSyncKey,KEY);
 }else if(name==='explicit_bad_key_preserves_everything'){
  await read(true,'short');assert.equal(calls.length,0);assert.equal(ctx.watchSyncKey,KEY);assert.deepEqual([...ctx.watch],['BTCUSDC']);
 }else throw Error(name);
 assert(calls.every(r=>r.url==='/api/watchlist'));assert(calls.every(r=>!r.opt.method||r.opt.method==='POST'));assert.equal((html.match(/<button\b[^>]*\bdata-tab="/g)||[]).length,4);console.log(name+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
