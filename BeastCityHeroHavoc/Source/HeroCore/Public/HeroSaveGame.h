#pragma once

#include "CoreMinimal.h"
#include "GameFramework/SaveGame.h"
#include "HeroTypes.h"
#include "HeroSaveGame.generated.h"

// One hero preset slot. UHeroProfileSubsystem persists/loads instances of this.
UCLASS()
class HEROCORE_API UHeroSaveGame : public USaveGame
{
	GENERATED_BODY()

public:
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Hero")
	FHeroProfile HeroProfile;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Hero")
	int32 SaveSlotVersion = 1;
};
