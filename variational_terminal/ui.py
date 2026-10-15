"""Brand the existing four-tab UI, with persistent reference-data disclosure."""
import re
from pathlib import Path

BANNER = '''<aside id="venueDisclosure" class="analyticsBox" role="note"><b>Variational Omni · 参考足で分析</b><p class="sub">市場価格・Funding・OIはVariationalの公開情報です。4H・15M・BTCチャートとEntry / SL / TP / RRはEdgeXの参考足で計算します。READYは参考足の条件成立で、Omniでのエントリー可否・約定・保護注文を保証しません。</p></aside>'''
API = '''<div class="analyticsBox" id="variationalApiInfo"><h3>Variational 市場データ</h3><p class="sub">公開統計APIを使用しています。取引・署名・口座APIには接続しません。提示quoteは最大600秒キャッシュされる参考値で、確約価格ではありません。価格の更新時刻が不明な場合は不明と表示します。</p><a href="https://omni.variational.io" target="_blank" rel="noopener noreferrer">Omniで価格・建玉・SL/TPを確認 ↗</a><div id="venueCoverage" class="status" role="status">確認中…</div><label for="venueSearch">Variationalの全銘柄を検索</label><input id="venueSearch" placeholder="BTC / ETH / 銘柄名" maxlength="64"><div id="venueMarketTable" class="scroll" style="max-height:360px"></div></div>'''
STYLE = '''<style>
#executionControlUnlock,#executionControlLock,#executionControlOn,#executionControlOff,#executionControlRefresh,#executionControlReconcile,#executionControlStatus,#executionControlNextStep,#accountViewDetails,#liveExecutionDetails,#capitalRefresh,#capitalStatus {display:none!important}
#api>.analyticsBox:has(#executionControlOn),#dashboard>.analyticsBox:has(#liveExecutionBadge){display:none!important}
#api>.dashboardHero>.sub {display:none}
#venueDisclosure{border-color:#7dd3fc;margin:12px 0}#venueDisclosure p{margin-bottom:0}
#variationalApiInfo input{width:100%;margin:10px 0}#venueMarketTable table{min-width:660px}
#venueAnalysis{border-color:#7dd3fc}
</style>'''


def render(html, *, btc=False):
    # The shared handler already rendered a nonce. A new CSP needs a new nonce.
    html = re.sub(r'<script\s+nonce="[^"]*">','<script>',html)
    html = html.replace('EdgeX','Variational').replace('EDGEX MARKET WORKSPACE','VARIATIONAL MARKET WORKSPACE')
    html = html.replace('edgex','variational')
    html = html.replace('pro.variational.exchange/en-US/portfolio/overview','omni.variational.io')
    html = html.replace('エントリー可能','参考条件成立').replace('ENTRY NOW','ENTRY NOW · 参考')
    html = html.replace('エントリー候補とAPI自動取引の稼働状態を確認できます。','Omniの市場情報とEdgeX参考足の条件成立を確認できます。')
    html = html.replace('v19.0.81','v1.0.0')
    html = html.replace('</head>',STYLE+'</head>')
    if btc:
        html = html.replace('<body>','<body>'+BANNER)
        return html
    html = html.replace('<div id="marketConnection"',BANNER+'<div id="marketConnection"',1)
    html = html.replace('<div id="analysisIdentity"','<div id="venueAnalysis" class="analyticsBox" role="status">参考足の銘柄を選ぶと、Omniの公開価格を併記します。</div><div id="analysisIdentity"',1)
    html = html.replace('<section id="api" class="panel">','<section id="api" class="panel">'+API,1)
    html = html.replace('<h2>接続・自動取引</h2>','<h2>接続・通知・模擬記録</h2>')
    html = html.replace('稼働状態と運用ルールを確認できます。自動取引をONにすると、開始後の現行READYから実注文を行います。','公開API・参考足・通知の接続状態を確認できます。自動取引は利用できません。')
    html = html.replace('仮想USDC口座の検証です。','EdgeX参考足による仮想USDC口座の検証です。Omniの約定成績ではありません。')
    html = html.replace('<option value="api"','<option disabled value="api"')
    html = html.replace('API欄の口座閲覧・自動取引操作は登録済みの所有端末限定です。','Variational版には口座閲覧・自動取引はありません。')
    html = html.replace('通知は現行READYのみ。','通知は参考足の現行READYのみ。')
    html = html.replace('</body>','<script>\n'+Path(__file__).with_name('venue.js').read_text()+'\n</script></body>')
    return html
