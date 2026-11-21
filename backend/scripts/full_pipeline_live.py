"""重跑 ASIN 与纯图片输入全链路，保留每一步结果，不复用候选或识别结果。"""
import json
import os
from datetime import datetime
from pathlib import Path

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.schemas import ProductInput, ProjectCreate
from app.domain.verification import VerificationInput
from app.services.images import ImageService
from app.services.product_understanding import ProductUnderstandingService
from app.services.qwen import QwenClient, QwenConfig
from app.services.roots import RootService
from app.services.verification import VerificationService

os.chdir(Path(__file__).resolve().parents[1])
os.environ.setdefault('KEYWORD_BROWSER_CHANNEL', 'chrome')
folder = Path('data') / ('full-pipeline-live-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
folder.mkdir(parents=True)
client = QwenClient(QwenConfig.load(Path('data')))
report = {'asin': 'B0CGV33WDY', 'marketplace': 'US', 'cases': []}


def save_report():
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


print('START ' + str(folder), flush=True)
for mode in ('asin', 'image'):
    case = {'mode': mode, 'stage': 'input', 'status': 'running'}
    report['cases'].append(case)
    save_report()
    repo = Repository(folder / mode / 'db.sqlite3')
    repo.initialize()
    project = repo.create_project(ProjectCreate(name=f'全链路实测 {mode} B0CGV33WDY', marketplace='US'))
    images = ImageService(repo, folder / mode / 'uploads')
    # 纯图片分支不传 ASIN、标题或描述，匹配结果再独立核对目标编号。
    reference_id = None
    if mode == 'image':
        source = Path('data/asin-B0CGV33WDY-20261024-085422/reference.jpg')
        image = images.save(project.id, 'reference.jpg', source.read_bytes(), 'reference')
        reference_id = image.id
        data = ProductInput(marketplace='US', image_ids=[image.id])
    else:
        data = ProductInput(marketplace='US', asin='B0CGV33WDY')
    run = repo.create_run(project.id, data, 'live-' + mode)
    understanding = ProductUnderstandingService(repo, images, client)
    roots = RootService(repo, understanding, client)
    service = VerificationService(repo, roots, images, model=client, auto_execute=False)
    last = ['']
    original_save = service.save

    def logged_save(task):
        original_save(task)
        (folder / mode / 'verification.json').write_text(task.model_dump_json(indent=2), encoding='utf-8')
        progress = [(i.text, i.status, i.active_query, i.checked_pages, len(i.image_comparisons)) for i in task.items]
        marker = str(progress)
        if marker != last[0]:
            last[0] = marker
            print(json.dumps({'mode': mode, 'progress': progress}, ensure_ascii=False), flush=True)

    service.save = logged_save
    try:
        case['stage'] = 'understanding'
        save_report()
        print(mode + ' UNDERSTANDING', flush=True)
        identified = understanding.identify(project.id, run.id)
        case['understanding'] = identified.model_dump(mode='json')
        case['stage'] = 'roots'
        save_report()
        candidates = roots.generate(project.id, run.id)
        case['roots'] = candidates.model_dump(mode='json')
        case['stage'] = 'verification'
        save_report()
        print(json.dumps({'mode': mode, 'candidates': [c.model_dump(mode='json') for c in candidates.candidates]}, ensure_ascii=False), flush=True)
        task = service.start(project.id, run.id, VerificationInput(reference_image_id=reference_id))
        service.execute(project.id, run.id, task.id)
        result = service.get(project.id, run.id, task.id)
        case['verification'] = result.model_dump(mode='json')
        case['status'] = result.status
        if result.selection_saved:
            selection = roots.get_selection(project.id, run.id)
            case['saved_roots'] = [c.text for c in candidates.candidates if c.id in selection.candidate_ids]
        case['target_asin_checks'] = [{'root': i.text, 'query': i.matched_query, 'matched_asin': i.matched_asin, 'is_expected_target': i.matched_asin == 'B0CGV33WDY'} for i in result.items if i.status == 'matched']
        case['stage'] = 'finished'
    except DomainError as error:
        case.update(status='failed', error_code=error.code, message=error.message)
        print(json.dumps({'mode': mode, 'stage': case['stage'], 'error': error.code, 'message': error.message}, ensure_ascii=False), flush=True)
    except Exception as error:
        case.update(status='failed', error_type=type(error).__name__)
        print(mode + ' ERROR ' + type(error).__name__, flush=True)
    finally:
        service.close()
        save_report()
    print(json.dumps({'mode': mode, 'status': case['status'], 'saved_roots': case.get('saved_roots', [])}, ensure_ascii=False), flush=True)
print('REPORT ' + str(folder / 'report.json'), flush=True)
