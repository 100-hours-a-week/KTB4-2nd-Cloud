FROM redgate/flyway@sha256:0472749995c526ef293a9d1651bc6af7e485def77fe7b9b23f223203af1cb5ac
LABEL org.opencontainers.image.source="https://github.com/100-hours-a-week/KTB4-2nd-Cloud"
COPY --chown=1001:0 sql/ /schema/sql/
COPY --chown=1001:0 source.lock.json checksums.sha256 migrate.sh /schema/
COPY --chown=1001:0 mysql-jdbc.jar /flyway/drivers/mysql-connector-j-9.7.0.jar
USER 1001
ENTRYPOINT ["/bin/bash", "/schema/migrate.sh"]
