// Core hero data types shared by the whole BeastCity: Hero Havoc project.
// Design lock: playable species are ONLY Raccoon, Chicken, Cat. Never add others here.
#pragma once

#include "CoreMinimal.h"
#include "HeroTypes.generated.h"

UENUM(BlueprintType)
enum class EHeroSpecies : uint8
{
	Raccoon UMETA(DisplayName = "Raccoon"),
	Chicken UMETA(DisplayName = "Chicken"),
	Cat UMETA(DisplayName = "Cat")
};

USTRUCT(BlueprintType)
struct HEROCORE_API FHeroAppearance
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Appearance")
	FLinearColor PrimaryColor = FLinearColor::White;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Appearance")
	FLinearColor SecondaryColor = FLinearColor::Gray;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Appearance")
	FLinearColor AccentColor = FLinearColor::Black;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Appearance")
	FName OutfitId = TEXT("Default");

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Appearance")
	TArray<FName> AccessoryIds;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Appearance")
	FName AuraStyleId = TEXT("None");
};

USTRUCT(BlueprintType)
struct HEROCORE_API FHeroPowerLoadout
{
	GENERATED_BODY()

	// Must contain exactly 2 entries - enforced by UHeroRulesLibrary::ValidateProfile.
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Powers")
	TArray<FName> ActiveIds;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Powers")
	FName UltimateId = NAME_None;

	// Must contain exactly 2 entries - enforced by UHeroRulesLibrary::ValidateProfile.
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Powers")
	TArray<FName> PassiveIds;
};

USTRUCT(BlueprintType)
struct HEROCORE_API FHeroProgression
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Progression")
	int32 Level = 1;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Progression")
	int32 XP = 0;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Progression")
	TArray<FName> UnlockFlags;
};

USTRUCT(BlueprintType)
struct HEROCORE_API FHeroProfile
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Hero")
	EHeroSpecies Species = EHeroSpecies::Raccoon;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Hero")
	FHeroAppearance Appearance;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Hero")
	FHeroPowerLoadout Powers;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Hero")
	FHeroProgression Progression;
};
