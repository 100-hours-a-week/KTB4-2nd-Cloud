const MAX_FILE_BYTES = 15 * 1024 * 1024;
const MAX_TOTAL_BYTES = 3 * 1024 * 1024 * 1024;

function validateEntry(entry, index) {
  if (!entry || typeof entry.path !== 'string' || !entry.path.startsWith('/')) {
    throw new Error(`files[${index}].path는 절대 경로여야 합니다.`);
  }
  if (entry.filename && /[\r\n"]/u.test(entry.filename)) {
    throw new Error(`files[${index}].filename에 사용할 수 없는 문자가 있습니다.`);
  }
  if (entry.contentType && !/^image\/[A-Za-z0-9.+-]+$/u.test(entry.contentType)) {
    throw new Error(`files[${index}].contentType은 image/* 형식이어야 합니다.`);
  }
}

function safeFilename(entry, index) {
  if (entry.filename) return entry.filename;
  const extension = entry.contentType === 'image/png' ? 'png' : 'jpg';
  return `photo-${String(index + 1).padStart(4, '0')}.${extension}`;
}

export function loadDataset(manifestPath) {
  const manifest = JSON.parse(open(manifestPath));
  if (!manifest || !Array.isArray(manifest.files)) {
    throw new Error('데이터 Manifest에는 files 배열이 필요합니다.');
  }
  if (manifest.files.length < 1 || manifest.files.length > 200) {
    throw new Error('사진 수는 1장 이상 200장 이하여야 합니다.');
  }

  let totalBytes = 0;
  const files = manifest.files.map((entry, index) => {
    validateEntry(entry, index);
    const data = open(entry.path, 'b');
    const sizeBytes = data.byteLength;
    if (sizeBytes < 1 || sizeBytes > MAX_FILE_BYTES) {
      throw new Error(`${entry.path} 크기가 1B 미만이거나 15MiB를 초과합니다.`);
    }
    if (entry.sizeBytes !== undefined && entry.sizeBytes !== sizeBytes) {
      throw new Error(`${entry.path}의 실제 크기와 Manifest sizeBytes가 다릅니다.`);
    }
    totalBytes += sizeBytes;
    return {
      data,
      filename: safeFilename(entry, index),
      contentType: entry.contentType || 'image/jpeg',
      sizeBytes,
    };
  });

  if (totalBytes > MAX_TOTAL_BYTES) {
    throw new Error('데이터셋 전체 크기가 3GiB를 초과합니다.');
  }

  return {
    name: manifest.name || 'unnamed-dataset',
    version: manifest.version || 'unversioned',
    files,
    totalBytes,
  };
}

export function splitBatches(files, batchSize = 10) {
  const batches = [];
  for (let index = 0; index < files.length; index += batchSize) {
    batches.push(files.slice(index, index + batchSize));
  }
  return batches;
}
