# Малый проверочный набор: 28 объектов и 4 района

Поставка разработчика из data.zip, получена 22.10.2026. Исходные objects.geojson,
regions.geojson, sources.json сохранены побайтово. Описание автора — SOURCE_README.md;
ссылки на scripts/docs в нём относятся к исходному проекту автора.
Снимок OSM и исходное описание перенесены вне Git в MDP/datasets/tomsk-demo-1/inputs
(tomsk-osm-2026-10-10.zip и verification-raw-README.md). Атрибуция:
© OpenStreetMap contributors, https://www.openstreetmap.org/copyright, ODbL 1.0.

Это проверочные данные: 6 школ, 5 медицинских учреждений, 8 аптек, 6 парков,
3 линейных объекта и 4 района. У части объектов бизнес-поля и/или геометрия
искусственные, что отмечено source/generated и synthetic_fields. Не представляйте
эти свойства как реальные сведения об учреждениях. Набор не выполняет требование
полного демонабора 500+4; не устанавливайте его как DEMO_PACKAGE_DIR.

## Файлы для административного импорта

Подготовка без доступа к БД из корня backend:

```powershell
python ops/prepare_verification.py
```

Результат в imports/: school.geojson (6), hospital.geojson (5), pharmacy.geojson (8),
park.geojson (6), improvement_zone.geojson (3), regions.geojson (4).
Каждый файл предназначен для одноимённого слоя. Для frontend успешного импорта:
`data/verification/imports/school.geojson` → слой `school`.
POST `/api/v1/admin/layers/school/import`, multipart file, admin, Origin и CSRF.
Импорт заменяет весь указанный слой. Использовать на согласованном локальном
проверочном стенде; не загружать вместо принятого полного набора.

Адаптация переносит properties.id в Feature.id, добавляет schema_version=1 и
описания используемых источников, разделяет категории; названия районов сокращает
до фиксированных имён backend (без « район»), region_code остаётся в исходнике.
ID, координаты, бизнес-поля, отметки синтетических полей не изменяются.
area_m2/length_m при импорте пересчитывает PostGIS штатным механизмом.
source-reference.json фиксирует контрольные суммы исходников для MLChat.
Исходные смешанные objects.geojson/regions.geojson непосредственно API не принимает.

MLChat использует эти исходники через scripts/sync_backend_verification.py;
его native acceptance fixtures сохраняют независимые ожидания. Старый валидатор
filter_validation и AST 1.0 не входят в действующий backend и хранятся отдельно.
