# kafka-topics --describe prints one Partition: line per partition.
# Require all three partition replicas to be caught up; a successful write alone is insufficient.
{
    partition = 0; replicas = 0; isr = 0
    for (i = 1; i <= NF; i++) {
        if ($i == "Partition:") partition = 1
        if ($i == "Replicas:") replicas = split($(i + 1), r, ",")
        if ($i == "Isr:") isr = split($(i + 1), s, ",")
    }
    if (partition) {
        count++
        if (replicas != 3 || isr != 3) bad++
    }
}
END { exit !(count == 3 && bad == 0) }
