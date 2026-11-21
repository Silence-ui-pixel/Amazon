"""复用已缓存主图和候选，实测图片输入的自然排名核验。"""
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from app.db.repository import Repository
from app.domain.roots import RootCandidate, RootCandidates
from app.domain.schemas import ProductInput, ProjectCreate
from app.domain.verification import VerificationInput
from app.services.images import ImageService
from app.services.product_understanding import ProductUnderstandingService
from app.services.qwen import QwenClient, QwenConfig
from app.services.roots import RootService
from app.services.verification import VerificationService

source = Path('data/asin-B0CGV33WDY-20261024-085422')
cached = json.loads((source / 'report.json').read_text(encoding='utf-8'))
folder = Path('data') / ('resilience-live-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
repo = Repository(folder / 'db.sqlite3')
repo.initialize()
project = repo.create_project(ProjectCreate(name='B0CGV33WDY 主图恢复实测',marketplace='US'))
images = ImageService(repo,folder / 'uploads')
image = images.save(project.id,'reference.jpg',(source / 'reference.jpg').read_bytes(),'reference')
run = repo.create_run(project.id,ProductInput(marketplace='US',image_ids=[image.id]),'live')
client = QwenClient(QwenConfig.load(Path('data')))
roots = RootService(repo,ProductUnderstandingService(repo,images,client),client)
candidates = RootCandidates(project_id=project.id,run_id=run.id,model='cached-qwen3.8-max',marketplace='US',language='en',created_at=datetime.now(UTC),warnings=['沿用此前候选'],candidates=[RootCandidate(id=uuid4(),**c) for c in cached['roots']['candidates']])
with repo.connect() as conn:
    conn.execute("INSERT INTO root_candidates VALUES (?,'succeeded',?)",(str(run.id),candidates.model_dump_json()))
service = VerificationService(repo,roots,images,model=client,auto_execute=False)
task = service.start(project.id,run.id,VerificationInput(reference_image_id=image.id))
(folder / 'task.json').write_text(task.model_dump_json(indent=2),encoding='utf-8')
print('START ' + str(folder) + ' task=' + str(task.id),flush=True)
try:
    service.execute(project.id,run.id,task.id)
    result = service.get(project.id,run.id,task.id)
    (folder / 'report.json').write_text(result.model_dump_json(indent=2),encoding='utf-8')
    print(json.dumps({'folder':str(folder),'status':result.status,'final':result.final,'items':[{'text':i.text,'status':i.status,'page':i.page,'position':i.position,'checked_pages':i.checked_pages,'comparisons':len(i.image_comparisons),'error_code':i.error_code} for i in result.items]},ensure_ascii=False),flush=True)
finally:
    service.close()
