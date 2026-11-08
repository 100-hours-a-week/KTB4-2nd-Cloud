# AI source a72b18ccc45a6385fb6e55ef161131de72e140a9, app/jobs/kafka.py
# AST-only regression fixture; do not import or execute.
def connection_options(cfg: JobsSettings) -> dict[str, Any]:
    """Producer와 Consumer가 같이 쓰는 연결 인자. 필수값 검사는 JobsSettings가 기동 때 마침

    SASL_SSL은 KAFKA_SSL_CAFILE로 서버 인증서와 호스트 이름을 검증한다. 검증 해제와
    PLAINTEXT 자동 전환은 두지 않는다.
    """
    options: dict[str, Any] = {
        "bootstrap_servers": cfg.KAFKA_BOOTSTRAP_SERVERS,
        "security_protocol": cfg.KAFKA_SECURITY_PROTOCOL,
    }
    if cfg.KAFKA_SECURITY_PROTOCOL == "SASL_SSL":
        assert cfg.KAFKA_SASL_PASSWORD is not None  # JobsSettings 검증기가 SASL_SSL이면 필수로 잡음
        options.update(
            ssl_context=ssl.create_default_context(cafile=cfg.KAFKA_SSL_CAFILE),
            sasl_mechanism=cfg.KAFKA_SASL_MECHANISM,
            sasl_plain_username=cfg.KAFKA_SASL_USERNAME,
            sasl_plain_password=cfg.KAFKA_SASL_PASSWORD.get_secret_value(),
        )
    return options
