"""验证已有复合候选的单词组成，按已知 ASIN 核对，不调用模型。"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from app.domain.errors import DomainError
from app.services.amazon_search import AmazonSearch
from app.services.search_policy import PAUSE_CODES

folder = Path('data') / ('atomic-roots-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
folder.mkdir(parents=True,exist_ok=True)
previous = Path('data/resilience-live-20261024-093247/search-evidence/6c3e76ea-e214-499c-bd3d-d7fdbc54c08e/browser-state.json')
if previous.is_file():
    shutil.copyfile(previous,folder/'browser-state.json')
report = {'asin':'B0CGV33WDY','marketplace':'US','zipcode':'10001','max_pages':3,'method':'exact_asin_in_organic_search','model_calls':0,'items':[]}

def save():
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

print('START '+str(folder),flush=True)
save()
try:
    with AmazonSearch().open('US',folder) as session:
        for word in ('wheel','caster','gate','support'):
            item={'text':word,'status':'running','pages':[]}
            report['items'].append(item)
            save()
            try:
                for number in range(1,4):
                    page=session.search(word,number,f'{word}-{number}')
                    item['pages'].append({'number':number,**page.model_dump()})
                    matches=[c for c in page.cards if c.asin==report['asin']]
                    if matches:
                        item.update(status='matched',page=number,position=matches[0].position,image_url=matches[0].image_url)
                    elif not page.next_page or number==3:
                        item['status']='not_found_in_scope'
                    save()
                    print(json.dumps({'word':word,'page':number,'cards':len(page.cards),'status':item['status'],'position':item.get('position'),'region':page.region},ensure_ascii=False),flush=True)
                    if item['status']!='running':
                        break
            except DomainError as error:
                item.update(status='failed',error_code=error.code,message=error.message)
                save()
                print(json.dumps({'word':word,'error':error.code},ensure_ascii=False),flush=True)
                if error.code in PAUSE_CODES:
                    report['status']='paused'
                    break
        else:
            report['status']='finished'
except DomainError as error:
    report.update(status='failed',error_code=error.code,message=error.message)
finally:
    save()
    print('REPORT '+str(folder/'report.json'),flush=True)
