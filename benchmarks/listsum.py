def count_long(words):
    n = 0
    for w in words:
        if len(w) > 3:
            n += 1
    return n


def main():
    words = []
    for i in range(2000):
        words.append("word" + str(i))
    total = 0
    for k in range(1000):
        total += count_long(words)
    print(total)


main()
