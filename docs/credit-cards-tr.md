# Kredi kartları, ekstreler ve taksitler

## Kart ekleme

**Kredi Kartlarım** bölümünden birden fazla kart ekleyin. Banka ve görünen kart adı zorunludur. Takma ad ve son dört hane isteğe bağlıdır. Uygulama tam kart numarası, CVV, PIN veya banka parolası istemez ya da saklamaz. Kart için iki aylık gün kuralı girilir: **Ekstre kesim günü** ve **Son ödeme günü** (ikisi de 1–31). Son ödeme günü kesim gününden küçükse vade sonraki aya, büyük veya eşitse aynı aya denk gelir. Örneğin kesim 28 ve ödeme 17 ise 28 Ekim ekstresinin vadesi 17 Kasım; kesim 5 ve ödeme 25 ise 5 Ekim ekstresinin vadesi 25 Ekim olur. İlgili ayda olmayan gün ayın son gününe uyarlanır. Kart özetinde sonraki kesim ve ödeme tarihleri ayrıca gösterilir.

## Harcama ve ekstre dönemi

Harcama formunda kart seçimi isteğe bağlıdır. Satın alma ekstre kesim gününde yapılırsa o gün kapanan ekstreye dahil edilir; sonraki gün yapılırsa sonraki döneme geçer. Örneğin kesim günü 31 olan kartta Şubat kesimi Şubatın son günüdür. Ekstre dönemi, önceki kesimin ertesi günü başlar. Kesim ve son ödeme tarihleri gerektiğinde ekstre satırından düzeltilebilir. Düzeltme, harcamanın saklanan ekstre kimliğini değiştirmez.

Ekstre toplamı tek çekim kart harcamalarını ve o döneme düşen taksitleri içerir. Bir taksitli alışveriş, harcama/bütçe raporunda satın alma gününde **tam tutarıyla tek kez** görünür; ekstrelerde ise tutarı aylara dağıtılmış şekilde görünür. Kuruş farkı varsa ilk taksitlere birer kuruş eklenerek tüm taksitlerin toplamı alışveriş tutarına eşitlenir.

## Ödeme ve borç

Kart ekranından ekstreye bağlı veya genel kart bakiyesine karşı kısmi/tam ödeme kaydedin. Ödeme, gider tablosuna eklenmez; dolayısıyla gider toplamını ve kategori bütçesini artırmaz. Kart borcu, açılış bakiyesi + kartla yapılan tam satın alma tutarları − kaydedilen ödemeler olarak gösterilir. Taksiti “Ödendi” olarak işaretlemek aynı tutarda kart ödemesi de kaydeder; bu işlem tekrar gönderilirse ikinci kez ödeme oluşturmaz.

## Veri ve sınırlamalar

- Kartlar, ekstreler, ödemeler ve taksitler hesap sahibine aittir. `due_day` ve ekstre tarih düzeltme işareti veritabanına eklemeli olarak eklenir; mevcut kart, harcama, ekstre, ödeme ve taksit kayıtları silinmez.
- Eski kartlarda yalnızca `due_days_after_close` varsa, geçiş mevcut kuralı kullanarak geçiş günündeki sonraki kesimi ve eski gün aralığını hesaplar; çıkan tarihin ayın günü `due_day` olarak kaydedilir. Bu, eski kuralın yaklaşık devamıdır ve bankanın gerçek ödeme gününü doğrulamaz. Geçişten sonra kart özetindeki ödeme gününü banka ekstresiyle kontrol edip gerekirse düzenleyin. Mevcut ekstre kesim/vade tarihleri aynen korunur.
- Kart ayarları değişince henüz kapanmamış, ödeme kaydı olmayan ve elle tarih düzeltmesi yapılmamış ileri tarihli ekstrelerin tarihleri yeni kurala göre güncellenir. Tarih düzeltmesi yapılan ekstreler, ödeme içeren ekstreler ve geçmiş ekstreler korunur. Harcamaların ekstre kimlikleri yeniden atanmaz.
- Kart ödemeleri nakit çıkış tarihiyle saklanır; uygulamada henüz açılış nakit bakiyesi/hesabı bulunmadığı için gerçek nakit bakiyesi uzlaştırması yapılmaz.
- CSV gider dışa/içe aktarımı kart bağlantılarını veya taksit planlarını taşımaz.
- Ekstre dönemleri, kaydedilmiş harcamalar veya taksitler olduğunda oluşturulur; boş aylar için boş ekstre üretimi yoktur.
- Bu özellik bankayla bağlantı kurmaz, hesap ekstresini doğrulamaz ve finansal tavsiye vermez.
