const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const context={fmt:v=>v==null?'—':String(v),directionJa:v=>v,reasonJa:v=>v||'reason',stageJa:v=>v};
vm.createContext(context);
vm.runInContext(script.slice(script.indexOf('function planAction('),script.indexOf('function reasonJa(')),context);
vm.runInContext(script.slice(script.indexOf('function planText('),script.indexOf('document.getElementById("analyze").onclick=')),context);
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
for(const {row,expected} of cases){
 const before=JSON.stringify(row),action=context.planAction(row),text=context.planText(row);
 assert.equal(action,expected.action);assert.equal(context.validFirstTarget(row),expected.first_target);
 assert(text.includes('最終利確 TP: '+row.take_profit));assert(text.includes('未成立の参考値'));
 if(expected.first_target==null)assert(!text.includes('第1利確 TP1:'));
 else assert(text.includes('第1利確 TP1: '+expected.first_target));
 if(action==='WAIT FOR STRUCTURE'){assert(text.includes('価格構造の更新待ち'));assert(!text.includes('15分足確定を待つ'));}
 assert.equal(JSON.stringify(row),before);
}
