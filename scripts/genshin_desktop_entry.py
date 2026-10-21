from startlux.scenarios.genshin.desktop import main, emit, error_fields

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit({"status": "error", **error_fields(error)})
        raise SystemExit(1)
