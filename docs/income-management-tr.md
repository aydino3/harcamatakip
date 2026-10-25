# Gelirleri kullanma

Dashboard'daki **Gelirler** alanından tek seferlik veya düzenli gelir ekleyin. Tür seçenekleri maaş, ek gelir, prim, serbest çalışma, iade/geri ödeme ve diğer gelirlerdir. Mevcut bir kategoriyi isteğe bağlı olarak seçebilirsiniz. Açıklama, tutar ve beklenen tarih girilir; tutarlar mevcut giderlerle aynı şekilde kuruş olarak saklanır.

## Beklenen ve alınan gelir

Yeni kayıt **Beklenen** durumundadır. Ödeme hesabınıza ulaştığında **Alındı olarak işaretle** seçeneğini kullanın. Uygulama gerçek alınma tarihini kaydeder. Hatalı tarih girildiyse **Alınma tarihini düzelt** seçeneğini kullanıp `YYYY-AA-GG` biçiminde yeni tarihi girin.

Beklenen gelir ile tahsil edilmiş gelir ayrı gösterilir. Beklenen gelir gider toplamına, bütçeye veya onaylanmış nakit bakiyesine eklenmez. Bu sürümde nakit bakiyesi hesabı bulunmadığından, dashboard'daki gelir özeti bir banka bakiyesi veya harcanabilir tutar değildir.

Beklenen tek seferlik kayıt düzenlenebilir veya silinebilir. Tahsil edilmiş kayıt geçmiş olarak korunur. Aktif düzenli kurallar duraklatılabilir, sürdürülebilir veya iptal edilebilir. Kuralın gelecek tutarını değiştirmek için **Gelecek tutarı değiştir** bölümünde yürürlük tarihi ve tutarı girin. Gelir CSV'si yalnızca oturum açmış hesabın gelir kayıtlarını indirir; kurallar ve giderler bu dosyaya dahil edilmez. Gider CSV iş akışı kendi dosyası olarak aynı şekilde çalışır.

## Düzenli gelir ve maaş tarihi

Sıklık olarak haftalık, aylık veya yıllık seçin. Maaş için aylık sıklık, başka bir gün biçimi seçilmediği sürece ayın son gününe ayarlanır. Kural Şubat, artık yıllar ve yıl geçişlerinde son takvim gününü kullanır. Örneğin Şubat 2024 için 29 Şubat, Şubat 2025 için 28 Şubat beklenir. Diğer aylık kurallar başlangıç gününe bağlı kalır; başlangıç 31 ise kısa aylarda o ayın son günü, sonraki uygun ayda yeniden 31 kullanılır.

Uygulama seçilen ayın ve ileriye dönük yaklaşık 45 günlük pencerenin beklenen kayıtlarını ekrana bakılırken oluşturur. Aynı kural/tarih için tekrar kayıt açılmaz. İptal edilmiş kural tekrar etkinleştirilemez; yeni kural oluşturun. Geçmişte alınmış gelirler geleceğe yönelik tutar değişikliğinden etkilenmez.

## Bilinen kapsam sınırları

Bu gelir özelliği henüz kişisel finans planlama sisteminin tamamı değildir. Kredi kartı hesabı/ekstresi, kart borcu ve geri ödemeleri, taksitler, açılış nakit bakiyesi, kronolojik ödeme takvimi, nakit akış tahmini ve finansal risk göstergeleri uygulanmış değildir. Dolayısıyla uygulama şu an maaş ve gider kayıtlarını birleştirip kullanılabilir para veya gelecekteki nakit yeterliliği hesaplamaz. Beklenen gelir, tahmin veya garanti anlamına gelmez.
