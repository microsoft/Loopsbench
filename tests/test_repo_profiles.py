from long_horizon_bench.test_env.repo_profiles import select_test_entry_files


def test_select_test_entry_files_accepts_ruby_test_files_for_rails():
    files = ["activerecord/test/cases/model_schema_type_for_column_test.rb"]

    assert select_test_entry_files("rails", files) is True
