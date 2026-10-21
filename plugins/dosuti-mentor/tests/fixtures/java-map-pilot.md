# Java: замена значения в HashMap

Источник: Oracle Java SE 21, HashMap.put(K,V), проверен 2026-10-21.
https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/HashMap.html#put(K,V)

Пересказ документации: put связывает значение с ключом. Если ключ уже есть,
его прежнее значение заменяется. size считает пары ключ–значение, а не вызовы put.
Это внешний справочник, а не запись преподавателя. Код и задания ниже — примеры наставника.

```java
import java.util.HashMap;
import java.util.Map;

class MapPilot {
    public static void main(String[] args) {
        Map<String, Integer> m = new HashMap<>();
        m.put("a", 2);
        m.put("b", 7);
        m.put("a", 5);
        System.out.println(m.get("a") + " " + m.get("b") + " " + m.size());
    }
}
```

Ожидаемый вывод: `5 7 2`. Почему размер остался равен двум?
Новая самостоятельная задача: put("x",4), put("y",8), put("x",9).
Назови get("x"), get("y") и size; объясни, что изменится при новом ключе "z".
