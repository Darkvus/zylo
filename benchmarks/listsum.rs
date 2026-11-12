fn count_long(words: &[String]) -> i64 {
    let mut n = 0;
    for w in words {
        if w.chars().count() > 3 {  // characters, like Python and Zylo (not bytes)
            n += 1;
        }
    }
    n
}

fn main() {
    let words: Vec<String> = (0..2000).map(|i| format!("word{}", i)).collect();
    let mut total: i64 = 0;
    for _ in 0..1000 {
        total += count_long(&words);
    }
    println!("{}", total);
}
