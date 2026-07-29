(module
  ;; 一个刻意保持极小的入口函数，用于观察解释器的取指、译码和执行。
  (func (export "main")
    (local $result i32)

    i32.const 1
    i32.const 2
    i32.add
    local.set $result

    local.get $result
    drop))
