"""One Windows profile for both PowerShell engines; native races stay separate."""
WINDOWS_MODULES = (
    'test_windows_runtime',
    'test_windows_onboarding',
    'test_platform_diagnostics',
    'test_history_read_only',
    'test_runtime_repair',
    'test_bootstrap',
    'test_python_probe',
    'test_import_rollback',
    'test_practice_reuse',
    'test_operation_reuse',
    'test_adaptive_practice',
    'test_learning_progress',
    'test_research_rollback',
    'test_history_cli_boundary',
    'test_write_preflight',
    'test_atomic_writes',
    'test_process_safety',
    'test_unit_event_input',
    'test_instruction_contracts',
    'test_html_content',
    'test_html_structure',
    'test_html_limits',
    'test_pdf_content',
    'test_pdf_import',
    'test_media_quality',
    'test_material_facts',
    'test_visual_materials',
    'test_quality_optimization',
    'test_source_library',
    'test_library_lesson',
    'test_context',
    'test_onboarding_hierarchy',
    'test_lesson_presentation',
    'test_windows_harness',
)


def load_tests(loader, tests, pattern):
    # Preserve unittest's failed-import/error handling and nonzero CLI exit.
    return loader.loadTestsFromNames(WINDOWS_MODULES)
