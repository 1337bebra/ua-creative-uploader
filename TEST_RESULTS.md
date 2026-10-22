# Результаты офлайн-тестирования

Отчёт автоматически сформирован `run_tests.py` по результатам фактического запуска.

Дата отчёта: 2026-10-22 (часовой пояс оператора).

**Итог: PASS.** Запущено 55 тестов; PASS: 55; FAIL/ERROR: 0; SKIP: 0.

Среда: Python 3.12.14, Windows. Время: 2.891 с.

## Что демонстрирует проверка

- Локальную логику загрузки MP4 и статики JPG/PNG, смешанные пачки и Google Drive source adapter.
- Формирование HTTP-параметров и multipart для поддельных Meta API-ответов.
- Дедупликацию по аккаунту и содержимому, сохранение video_id/image_hash и результатов каждого задания.
- Обработку таймаутов, незавершённых операций, ошибок реестра и восстановление без слепого повторного POST.
- Валидацию файлов, конфигурации, разделение credentials и отсутствие токена в URL/сообщениях ошибок.

Сетевые socket-соединения заблокированы на весь прогон. Реальные кабинеты, токены и расходы не используются.

## Границы доказательства

Это подтверждение локальных сценариев и контрактов на fake API, а не доказательство успешной работы с живой Meta/Google. Не проверены реальные права приложения, принятие payload текущей API-версией, кодирование видео и допустимость изображений для placements. PNG генерируется как маленькое настоящее изображение; JPEG и MP4 fixtures проверяют маркеры/заголовки и маршрутизацию, а не декодирование медиа. Полное декодирование файлов загрузчик не выполняет.

## Воспроизведение

```powershell
python run_tests.py --report TEST_RESULTS.md
```

Не нужны токены, аккаунты или сторонние Python-библиотеки. Команда возвращает 0 при успехе и 1 при провале.

## Каждый тест

| Тест | Результат |
|---|---|
| `test_images.ImageTests.test_adopt_wrong_account_image_is_rejected` | PASS |
| `test_images.ImageTests.test_bad_png_crc_truncation_and_animated_png_are_rejected` | PASS |
| `test_images.ImageTests.test_drive_static_batch_routes_image` | PASS |
| `test_images.ImageTests.test_drive_static_download_and_mime_spoofing` | PASS |
| `test_images.ImageTests.test_extension_content_mismatch_is_rejected` | PASS |
| `test_images.ImageTests.test_image_crash_intent_is_not_recreated` | PASS |
| `test_images.ImageTests.test_image_post_timeout_never_blindly_retries` | PASS |
| `test_images.ImageTests.test_image_preflight_requests_only_image_library` | PASS |
| `test_images.ImageTests.test_image_read_failure_retains_hash_and_recovers_without_post` | PASS |
| `test_images.ImageTests.test_images_dedupe_content_and_keep_all_results` | PASS |
| `test_images.ImageTests.test_inactive_missing_and_malformed_image_listing_are_not_ready` | PASS |
| `test_images.ImageTests.test_meta_image_protocol_and_secret_handling` | PASS |
| `test_images.ImageTests.test_missing_or_ambiguous_upload_hash_is_rejected` | PASS |
| `test_images.ImageTests.test_mixed_batch_routes_video_and_image_separately` | PASS |
| `test_images.ImageTests.test_offline_image_plan_and_manifest_compatibility` | PASS |
| `test_images.ImageTests.test_png_and_jpeg_uploads_preserve_type_and_remote_hash` | PASS |
| `test_images.ImageTests.test_reconcile_and_adopt_images` | PASS |
| `test_images.ImageTests.test_same_title_changed_content_is_new_and_accounts_are_isolated` | PASS |
| `test_images.ImageTests.test_truncated_jpeg_and_unsupported_content_are_rejected` | PASS |
| `test_uploader.BatchTests.test_all_duplicates_and_trailing_duplicate_are_persisted` | PASS |
| `test_uploader.BatchTests.test_archive_failure_stops_and_leaves_intent_for_recovery` | PASS |
| `test_uploader.BatchTests.test_atomic_registry_and_audit` | PASS |
| `test_uploader.BatchTests.test_crash_intent_blocks_recreation` | PASS |
| `test_uploader.BatchTests.test_empty_batch_always_writes_report` | PASS |
| `test_uploader.BatchTests.test_local_missing_file_preserves_report_and_continues` | PASS |
| `test_uploader.BatchTests.test_no_path_or_title_becomes_staging_filename` | PASS |
| `test_uploader.BatchTests.test_processing_failure_does_not_auto_reupload` | PASS |
| `test_uploader.BatchTests.test_processing_is_not_success_and_does_not_upload_again` | PASS |
| `test_uploader.BatchTests.test_same_bytes_different_accounts_upload_separately` | PASS |
| `test_uploader.BatchTests.test_same_title_different_bytes_upload_separately` | PASS |
| `test_uploader.BatchTests.test_second_state_lock_fails` | PASS |
| `test_uploader.BatchTests.test_source_failure_continues_other_jobs` | PASS |
| `test_uploader.BatchTests.test_status_read_error_retains_remote_id_and_does_not_upload_again` | PASS |
| `test_uploader.BatchTests.test_unknown_write_outcomes_are_not_retried` | PASS |
| `test_uploader.DriveTests.test_drive_changed_source_or_partial_content_rejected` | PASS |
| `test_uploader.DriveTests.test_drive_download_validates_content` | PASS |
| `test_uploader.InputTests.test_duplicate_jobs_unknown_profile_and_unsafe_id_rejected` | PASS |
| `test_uploader.InputTests.test_header_and_size_validation` | PASS |
| `test_uploader.InputTests.test_independent_profile_credentials` | PASS |
| `test_uploader.InputTests.test_invalid_or_unknown_config_fields_rejected` | PASS |
| `test_uploader.InputTests.test_offline_plan_requires_no_credentials_or_network` | PASS |
| `test_uploader.InputTests.test_partial_upload_cli_returns_nonzero` | PASS |
| `test_uploader.InputTests.test_relative_source_paths_use_manifest_location` | PASS |
| `test_uploader.RecoveryTests.test_adopt_checks_account_membership` | PASS |
| `test_uploader.RecoveryTests.test_deleted_or_unreadable_known_object_requires_explicit_resolution` | PASS |
| `test_uploader.RecoveryTests.test_reconcile_ready_resolves_uncertainty` | PASS |
| `test_uploader.RecoveryTests.test_reconcile_unknown_processing_does_not_confirm_finish` | PASS |
| `test_uploader.RecoveryTests.test_reset_rejects_ready_and_records_explicit_recovery` | PASS |
| `test_uploader.TransportTests.test_app_secret_proof_for_get_and_multipart` | PASS |
| `test_uploader.TransportTests.test_http_token_is_header_only_and_post_not_retried` | PASS |
| `test_uploader.TransportTests.test_invalid_processing_response_is_not_silently_pending` | PASS |
| `test_uploader.TransportTests.test_listing_does_not_follow_remote_next_url_or_assume_complete` | PASS |
| `test_uploader.TransportTests.test_meta_adapter_end_to_end_protocol_with_fake_http` | PASS |
| `test_uploader.TransportTests.test_read_retry_bounded` | PASS |
| `test_uploader.TransportTests.test_streamed_multipart_content_length_and_slice` | PASS |

## Привязка к исходникам

SHA-256 набора Python-файлов позволяет отличить проверенную версию от последующих изменений; это контрольная сумма, не независимая сертификация.

`06fbdcf7d2bfe163d89cc53c8c22d5af6fd28bd94415d58bc1879d870a63d9a7`

| Файл | SHA-256 |
|---|---|
| `common.py` | `f3494e6a9a7ae8dd461cfc3e1629716f9d6f9fd4a94174626dc4b5db823ee2e7` |
| `creative_archive.py` | `d176fd2e2ed877fbbb69569d97eb3860f46f8680da4319fd6e816046bd721725` |
| `drive_fetch.py` | `02ac960c8d9090b582cb555679bfee15c9a3a173e33c2a8ee0a286e2f46cdaac` |
| `make_release.py` | `952a7ac4e747b3dd76d305ff5184c434e09e34a54fe7cf408eb70b10c088fcef` |
| `meta_api.py` | `d60bbd59972d52654bf2e3451eb306ca4298f5b99406670b81df7cd07433d270` |
| `meta_secrets.py` | `4c6b2aa340881740fd54fdcdfc359a58c8129f953a87c44b2ff721ebc5f039a6` |
| `meta_upload.py` | `7c42e3d780a6f9d4e2dd44fd6f6704da59c00ce7da412efa7cec7abc1e91b941` |
| `run_tests.py` | `827b835c7cde6119dd1201a4c049419a7146dd6a34e0b36e567ce5a3ccf57021` |
| `tests/test_images.py` | `1b7aaf18d6cfe29633fa50b286eb0f66366f82b5f706bc93f2a7af1ea0501cac` |
| `tests/test_uploader.py` | `d110efd7eb498b7d21bbfe621da5eafd187536b2e005b700e87dadfa9d9a025e` |
