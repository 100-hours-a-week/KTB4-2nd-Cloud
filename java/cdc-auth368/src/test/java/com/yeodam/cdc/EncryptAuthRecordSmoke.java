package com.yeodam.cdc;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Base64;
import java.util.Map;
import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import org.apache.kafka.connect.data.Schema;
import org.apache.kafka.connect.source.SourceRecord;

public final class EncryptAuthRecordSmoke {
    public static void main(String[] args) throws Exception {
        String topic = "staging.yeodam.migration.auth368.encrypted";
        byte[] key = new byte[32];
        java.util.Arrays.fill(key, (byte) 7);
        Path keyFile = Files.createTempFile("auth368-smoke-", ".hex");
        try {
            Files.writeString(keyFile, java.util.HexFormat.of().formatHex(key));
            EncryptAuthRecord<SourceRecord> transform = new EncryptAuthRecord<>();
            transform.configure(Map.of("key.file", keyFile.toString(), "destination.topic", topic));
            SourceRecord input = new SourceRecord(Map.of("server", "test"), Map.of("offset", 1),
                    "staging.yeodam.migration.auth368.source.yeodam_v1_source.csrf_tokens",
                    Schema.STRING_SCHEMA, "context-hash", Schema.STRING_SCHEMA, "secret-token-value");
            SourceRecord encrypted = transform.apply(input);
            if (!topic.equals(encrypted.topic()) || !"auth".equals(encrypted.key()) ||
                    !"0".equals(String.valueOf(encrypted.kafkaPartition())) ||
                    String.valueOf(encrypted.value()).contains("secret-token-value")) {
                throw new AssertionError("topic, partition, fixed key or ciphertext contract failed");
            }
            byte[] packed = Base64.getDecoder().decode(String.valueOf(encrypted.value()).substring(3));
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"),
                    new GCMParameterSpec(128, packed, 0, 12));
            cipher.updateAAD(topic.getBytes(StandardCharsets.UTF_8));
            byte[] clear = cipher.doFinal(packed, 12, packed.length - 12);
            if (!new String(clear, StandardCharsets.UTF_8).contains("secret-token-value")) {
                throw new AssertionError("encrypted record did not recover original event");
            }
            if (transform.apply(new SourceRecord(Map.of(), Map.of(), input.topic(), null, null)) != null) {
                throw new AssertionError("raw tombstone escaped");
            }
            transform.close();
            System.out.println("AUTH368_SMT_SMOKE_OK");
        } finally {
            Files.deleteIfExists(keyFile);
        }
    }
}
