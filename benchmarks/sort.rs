fn make(n: usize) -> Vec<i64> {
    let mut xs = Vec::new();
    let mut seed: i64 = 12345;
    for _ in 0..n {
        seed = (seed * 1103515245 + 12345) % 2147483648;
        xs.push(seed % 100000);
    }
    xs
}

fn bubble_sort(mut xs: Vec<i64>) -> Vec<i64> {
    let n = xs.len();
    for i in 0..n {
        for j in 0..n - 1 - i {
            if xs[j] > xs[j + 1] {
                xs.swap(j, j + 1);
            }
        }
    }
    xs
}

fn main() {
    let data = bubble_sort(make(3000));
    println!("{}", data[0]);
    println!("{}", data[1500]);
    println!("{}", data[2999]);
}
