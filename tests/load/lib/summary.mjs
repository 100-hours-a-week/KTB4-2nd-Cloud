export function summaryOutput(data) {
  const outputPath = __ENV.K6_SUMMARY_PATH || 'results/summary.json';
  return {
    stdout: `\n여담 k6 실행 요약을 ${outputPath}에 저장했습니다.\n`,
    [outputPath]: JSON.stringify(data, null, 2),
  };
}

export function emitRunEvent(event) {
  console.log(JSON.stringify({ source: 'yeodam-k6', ...event }));
}
